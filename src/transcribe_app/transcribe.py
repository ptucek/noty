"""Audio → seznam NoteEvent + audio metadata (tempo). Router podle kategorie hudby."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .notation import NoteEvent

logger = logging.getLogger(__name__)

# Default vocal-separation model (audio-separator zná tento filename).
# BS-Roformer-Viperx-1297: vokál SDR 11.77 dB, ~600 MB checkpoint, stažen při prvním použití
# a cachován v ~/Library/Caches/audio-separator (resp. ./models). Lepší než htdemucs (~9 dB).
ROFORMER_MODEL_FILENAME = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"

Category = Literal["monofonni", "klavir", "kapela", "vokal"]
CATEGORIES: tuple[Category, ...] = ("monofonni", "klavir", "kapela", "vokal")

# Aktuálně používáme jen Basic Pitch. Aria-AMT / YourMT3+ scaffolding byl odstraněn
# (CUDA-only, neintegrovatelné na CPU — viz docs/INITIAL_PLAN.md commit history).


@dataclass(frozen=True)
class TranscriptionResult:
    events: list[NoteEvent]
    tempo_bpm: float
    category: str
    key_sharps: int
    key_mode: str  # "major" / "minor"
    key_name: str  # např. "G major"
    time_signature: str  # např. "3/4" / "4/4"


def transcribe(
    audio_path: Path,
    category: Category,
    output_dir: Path,
    *,
    tempo_override: float | None = None,
    key_sharps_override: int | None = None,
    time_signature_override: str | None = None,
) -> TranscriptionResult:
    """Audio → události + tempo + tónina + takt. Volitelné override hodnoty obejdou auto-detekci.

    Pokud je override vyplněn (uživatel zná správnou hodnotu), použije se místo detekce.
    Pro 'vokal' nejdřív Demucs/RoFormer vokál izolace.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # Načti audio ONCE — všechny 3 detektory (tempo/key/time-sig) sdílí stejný stream.
    import librosa
    y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
    tempo_detected, beat_times = _detect_tempo_and_beats(audio_path, y=y, sr=sr)
    key_sharps_detected, key_mode, key_name, key_candidates = _detect_key_full(audio_path, y=y, sr=sr)
    time_sig_detected, time_sig_contrasts = _detect_time_signature_full(audio_path, y=y, sr=sr)

    tempo_bpm = float(tempo_override) if tempo_override is not None else tempo_detected
    key_sharps = int(key_sharps_override) if key_sharps_override is not None else key_sharps_detected
    time_sig = time_signature_override if time_signature_override else time_sig_detected
    if tempo_override is not None:
        logger.info("Tempo override: %.1f (auto detected %.1f)", tempo_bpm, tempo_detected)
    if key_sharps_override is not None:
        key_name = _major_key_name_for_sharps(key_sharps)
        key_mode = "major"
        logger.info("Key override: %s (%+d sharps, auto detected %+d)", key_name, key_sharps, key_sharps_detected)
    if time_signature_override:
        logger.info("Time signature override: %s (auto detected %s)", time_sig, time_sig_detected)
    source = _isolate_vocals(audio_path, output_dir) if category == "vokal" else audio_path
    events_raw = _basic_pitch_events(source)
    events = _clean_events(events_raw, category)

    extra_context = {
        "audio_duration_s": round(float(max((e.end_s for e in events_raw), default=0.0)), 2),
        "librosa_beat_times_first20": [round(t, 3) for t in beat_times[:20]],
        "librosa_beat_count": len(beat_times),
        "librosa_time_signature_contrasts": time_sig_contrasts,
        "key_candidates_top3": [
            {"key": name, "sharps": sh, "ks_score": sc} for name, sh, sc in key_candidates
        ],
        "heuristic_cleanup_stats": {
            "raw_event_count": len(events_raw),
            "after_cleanup": len(events),
            "filtered_out": len(events_raw) - len(events),
        },
        "backends": {
            "transcription": "basic_pitch",
            "separation": os.environ.get("SEPARATION_BACKEND", "roformer") if category == "vokal" else "n/a",
        },
    }

    from .llm_cleanup import apply_cleanup, cleanup_with_llm

    cleanup = cleanup_with_llm(
        events, tempo_bpm, key_sharps, key_name, time_sig, category, extra_context=extra_context
    )
    if cleanup is not None:
        events = apply_cleanup(events, cleanup.suggestion)
        # Manuální override má vždy přednost před LLM návrhem.
        if tempo_override is None:
            tempo_bpm = float(cleanup.suggestion.tempo_bpm)
        if key_sharps_override is None:
            key_sharps = cleanup.suggestion.key_sharps
        # Time signature záměrně NEPŘEPISUJEME LLM — librosa baseline je spolehlivější
        # (testy: 51% accuracy bez LLM vs 35% s LLM cleanup).
        logger.info("LLM cleanup aplikován: %s", cleanup.suggestion.rationale)

    logger.info(
        "Transkripce hotová: %d not, tempo %.1f BPM, %s, takt %s",
        len(events), tempo_bpm, key_name, time_sig,
    )
    return TranscriptionResult(
        events=events,
        tempo_bpm=tempo_bpm,
        category=category,
        key_sharps=key_sharps,
        key_mode=key_mode,
        key_name=key_name,
        time_signature=time_sig,
    )


def _detect_tempo_and_beats(
    audio_path: Path, y=None, sr: int | None = None
) -> tuple[float, list[float]]:
    """Tempo detection s octave-aware corrections.

    Pokud ``y`` a ``sr`` jsou předány, použijí se přímo (úspora — žádný re-load).
    Jinak se audio načte z ``audio_path``.

    librosa.beat.beat_track občas vrátí 2× nebo 0.5× true tempo (octave error).
    Pokud detekované tempo je > 140 BPM a polovina je v [60, 140], halvuje.
    """
    import librosa

    try:
        if y is None or sr is None:
            y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        tempo, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
        tempo_val = float(tempo) if hasattr(tempo, "__float__") else float(tempo[0])
        beat_times = librosa.frames_to_time(beats, sr=sr).tolist() if len(beats) > 0 else []

        # Octave-aware correction: pokud > 140, zkus halvovat
        if 140 < tempo_val <= 280 and 60 <= tempo_val / 2 <= 140:
            halved = tempo_val / 2
            logger.info("Octave correction: %.1f → %.1f BPM (halved)", tempo_val, halved)
            tempo_val = halved
        # ... nebo doublovat, pokud < 50 a 2× je v rozsahu
        elif 30 <= tempo_val < 50 and 60 <= tempo_val * 2 <= 140:
            doubled = tempo_val * 2
            logger.info("Octave correction: %.1f → %.1f BPM (doubled)", tempo_val, doubled)
            tempo_val = doubled

        if tempo_val < 40 or tempo_val > 240:
            logger.warning("Tempo %.1f mimo rozumný rozsah, default 120", tempo_val)
            return 120.0, beat_times
        return tempo_val, beat_times
    except Exception as exc:
        logger.warning("Tempo detection selhala: %s, default 120", exc)
        return 120.0, []


_SHARPS_TO_MAJOR_TONIC = {
    -7: "C♭", -6: "G♭", -5: "D♭", -4: "A♭", -3: "E♭", -2: "B♭", -1: "F",
    0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#", 7: "C#",
}


def _major_key_name_for_sharps(sharps: int) -> str:
    tonic = _SHARPS_TO_MAJOR_TONIC.get(sharps, "C")
    return f"{tonic} major"


# Krumhansl-Schmuckler key profily (Krumhansl 1990).
_KS_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
_KS_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
_PITCH_NAMES = ("C", "C#", "D", "E♭", "E", "F", "F#", "G", "A♭", "A", "B♭", "B")
# pc → počet křížků v daném major key
_MAJOR_SHARPS = {0: 0, 1: 7, 2: 2, 3: -3, 4: 4, 5: -1, 6: 6, 7: 1, 8: -4, 9: 3, 10: -2, 11: 5}


def _detect_key_full(
    audio_path: Path, y=None, sr: int | None = None,
) -> tuple[int, str, str, list[tuple[str, int, float]]]:
    """Krumhansl-Schmuckler. Vrátí (sharps, mode, name, top3_candidates)."""
    import librosa
    import numpy as np

    try:
        if y is None or sr is None:
            y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        avg_chroma = chroma.mean(axis=1)
        if avg_chroma.sum() <= 0:
            return 0, "major", "C major", []
        avg_norm = avg_chroma - avg_chroma.mean()

        scored: list[tuple[float, int, str, int]] = []
        for shift in range(12):
            rotated = np.roll(avg_norm, -shift)
            for mode_name, template in (("major", _KS_MAJOR), ("minor", _KS_MINOR)):
                t = np.array(template) - np.mean(template)
                score = float(np.dot(rotated, t))
                sharps = _MAJOR_SHARPS[shift] if mode_name == "major" else _MAJOR_SHARPS[(shift + 3) % 12]
                scored.append((score, shift, mode_name, sharps))

        scored.sort(reverse=True)
        top = [
            (f"{_PITCH_NAMES[pc]} {m}", sh, round(sc, 3))
            for sc, pc, m, sh in scored[:3]
        ]
        _, best_pc, best_mode, best_sharps = scored[0]
        key_name = f"{_PITCH_NAMES[best_pc]} {best_mode}"
        return best_sharps, best_mode, key_name, top
    except Exception as exc:
        logger.warning("Key detection selhala: %s, default C major", exc)
        return 0, "major", "C major", []


def _basic_pitch_events(audio_path: Path) -> list[NoteEvent]:
    """Spustí Basic Pitch, vrátí list NoteEvent."""
    from basic_pitch import ICASSP_2022_MODEL_PATH
    from basic_pitch.inference import predict

    logger.info("Basic Pitch transkripce: %s", audio_path.name)
    _, _midi_data, note_events = predict(
        str(audio_path),
        model_or_model_path=ICASSP_2022_MODEL_PATH,
    )
    events = [
        NoteEvent(
            pitch_midi=int(pitch),
            start_s=float(start),
            end_s=float(end),
            velocity=max(1, min(127, int(amp * 127))),
        )
        for (start, end, pitch, amp, _bends) in note_events
    ]
    return events


def _isolate_vocals(audio_path: Path, output_dir: Path) -> Path:
    """Izoluje vokál → WAV path. Default Mel-Band/BS RoFormer, fallback Demucs.

    Backend volitelný env var ``SEPARATION_BACKEND`` (``roformer`` | ``demucs``).
    Pokud RoFormer (audio-separator) selže, automatický fallback na Demucs.
    """
    backend = os.environ.get("SEPARATION_BACKEND", "roformer").lower()
    if backend == "roformer":
        try:
            return _separate_with_roformer(audio_path, output_dir)
        except Exception as exc:  # pragma: no cover - fallback path
            logger.warning("RoFormer selhal (%s), fallback na Demucs", exc)
    return _separate_with_demucs(audio_path, output_dir)


def _separate_with_roformer(audio_path: Path, output_dir: Path) -> Path:
    """audio-separator + BS-Roformer (SOTA 2024). Vrátí cestu k vokál WAV.

    První volání stáhne checkpoint (~600 MB) do cache; další běhy už ne.
    Pro krátké klipy (<10 s) audio předpadujeme nulami na min. ~12 s, protože
    upstream demix() neumí spočítat overlap-add když je vstup kratší než chunk
    (`mix.shape < chunk_size`). Po separaci ořežeme zpět na původní délku.
    """
    import shutil

    import numpy as np
    import soundfile as sf
    from audio_separator.separator import Separator

    logger.info("RoFormer vokál izolace: %s", audio_path.name)

    # Detekuj délku; pokud krátká, vytvoř padded kopii v output_dir.
    info = sf.info(str(audio_path))
    orig_frames = info.frames
    orig_sr = info.samplerate
    MIN_DURATION_S = 12.0
    min_frames = int(MIN_DURATION_S * orig_sr)
    work_input = audio_path
    pad_frames = 0
    if orig_frames < min_frames:
        data, sr = sf.read(str(audio_path), always_2d=True)
        pad_frames = min_frames - orig_frames
        pad = np.zeros((pad_frames, data.shape[1]), dtype=data.dtype)
        data_padded = np.concatenate([data, pad], axis=0)
        work_input = output_dir / "_roformer_input_padded.wav"
        sf.write(str(work_input), data_padded, sr)

    separator = Separator(
        log_level=logging.WARNING,
        output_dir=str(output_dir),
        output_format="WAV",
    )
    separator.load_model(model_filename=ROFORMER_MODEL_FILENAME)
    output_files = separator.separate(str(work_input))

    # Najdi vokální stopu (název obsahuje "vocals", ale ne "instrumental").
    vocal_file: Path | None = None
    for name in output_files:
        path = (output_dir / name) if not Path(name).is_absolute() else Path(name)
        lo = path.name.lower()
        if "vocals" in lo and "instrumental" not in lo and "no_vocals" not in lo:
            vocal_file = path
            break
    if vocal_file is None or not vocal_file.exists():
        raise RuntimeError(f"RoFormer nevytvořil vokál stopu (výstupy: {output_files})")

    canonical = output_dir / "vocals.wav"
    if pad_frames > 0:
        # Načti a ořež zpět na původní počet vzorků.
        data, sr = sf.read(str(vocal_file), always_2d=True)
        data = data[:orig_frames]
        sf.write(str(canonical), data, sr)
    elif vocal_file.resolve() != canonical.resolve():
        shutil.copyfile(vocal_file, canonical)
    return canonical


def _separate_with_demucs(audio_path: Path, output_dir: Path) -> Path:
    """Demucs htdemucs → uloží vokální stopu jako WAV, vrátí cestu. Fallback path."""
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.audio import convert_audio
    from demucs.pretrained import get_model

    logger.info("Demucs vokál izolace: %s", audio_path.name)
    model = get_model("htdemucs")
    model.eval()
    data, sr = sf.read(str(audio_path), always_2d=True)
    wav = torch.from_numpy(data.T.astype(np.float32))
    wav = convert_audio(wav, sr, model.samplerate, model.audio_channels)
    sources = apply_model(model, wav[None], device="cpu", progress=False)[0]
    vocals_idx = model.sources.index("vocals")
    vocals = sources[vocals_idx].cpu().numpy().T
    vocal_path = output_dir / "vocals.wav"
    sf.write(str(vocal_path), vocals, model.samplerate)
    return vocal_path


def _keep_highest_per_onset(events: list[NoteEvent], window_s: float = 0.05) -> list[NoteEvent]:
    """Pro monofonní výstup: ze sousedních onsetů (do 50 ms) nech jen nejvyšší pitch."""
    if not events:
        return events
    sorted_evs = sorted(events, key=lambda e: e.start_s)
    out: list[NoteEvent] = []
    for ev in sorted_evs:
        if out and abs(ev.start_s - out[-1].start_s) < window_s:
            if ev.pitch_midi > out[-1].pitch_midi:
                out[-1] = ev
        else:
            out.append(ev)
    return out


# ---------- Post-processing heuristiky (Phase A) ----------

MIN_NOTE_DURATION_S = 0.05  # < 50 ms = pravděpodobně šum Basic Pitch
MIN_VELOCITY = 15  # noty pod tímto practicky neslyšet → pravděpodobně false positive
OCTAVE_DUP_WINDOW_S = 0.03  # do 30 ms = stejný onset, jen oktávová duplikace


def _clean_events(events: list[NoteEvent], category: Category) -> list[NoteEvent]:
    """Aplikuje heuristiky: krátké noty pryč, tiché pryč, oktávové dublikáty pryč."""
    if not events:
        return events
    before = len(events)
    events = [e for e in events if (e.end_s - e.start_s) >= MIN_NOTE_DURATION_S]
    events = [e for e in events if e.velocity >= MIN_VELOCITY]
    events = _suppress_octave_duplicates(events)
    if category == "monofonni":
        events = _keep_highest_per_onset(events)
    logger.info("Cleanup: %d → %d not (-%d)", before, len(events), before - len(events))
    return events


def _suppress_octave_duplicates(events: list[NoteEvent]) -> list[NoteEvent]:
    """Když na stejném onsetu zní P i P+12 (oktáva), výše položená je obvykle harmonika P → pryč."""
    if not events:
        return events
    sorted_evs = sorted(events, key=lambda e: e.start_s)
    keep = [True] * len(sorted_evs)
    for i, ev_i in enumerate(sorted_evs):
        if not keep[i]:
            continue
        for j in range(i + 1, len(sorted_evs)):
            ev_j = sorted_evs[j]
            if ev_j.start_s - ev_i.start_s > OCTAVE_DUP_WINDOW_S:
                break
            if ev_j.pitch_midi == ev_i.pitch_midi + 12:
                keep[j] = False
    return [e for e, k in zip(sorted_evs, keep) if k]


def _detect_time_signature_full(
    audio_path: Path, y=None, sr: int | None = None,
) -> tuple[str, dict[str, float]]:
    """Vrátí (best_meter, all_contrasts_dict). Kandidáti: 2/4, 3/4, 4/4, 6/8.

    Contrast = downbeat_strength / mean(other_beat_strengths).
    Pro 6/8 testujeme dva 3-beat trsy (2 silné doby v taktu).
    """
    import librosa
    import numpy as np

    try:
        if y is None or sr is None:
            y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        _, beats = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr)
        if len(beats) < 8:
            return "4/4", {}
        strengths = onset_env[beats]
        contrasts: dict[str, float] = {}
        for label, meter in (("2/4", 2), ("3/4", 3), ("4/4", 4), ("6/8", 6)):
            if label == "6/8":
                # 6/8 = dva 3-beat trsy: silné na pozici 0 a 3
                n_full = len(strengths) // 6
                if n_full < 2:
                    continue
                grouped = strengths[: n_full * 6].reshape(n_full, 6)
                downbeats = (grouped[:, 0].mean() + grouped[:, 3].mean()) / 2
                others = np.concatenate([grouped[:, 1:3].flatten(), grouped[:, 4:6].flatten()]).mean()
                contrasts[label] = float(downbeats / max(others, 1e-6))
            else:
                n_full = len(strengths) // meter
                if n_full < 2:
                    continue
                grouped = strengths[: n_full * meter].reshape(n_full, meter)
                downbeat = float(grouped[:, 0].mean())
                other = float(grouped[:, 1:].mean()) if meter > 1 else 0.0
                contrasts[label] = downbeat / max(other, 1e-6)

        # Vyber nejlepší kandidát. Aplikujeme tie-breaking proti menším metrům:
        # - 2/4 statisticky "vyhraje" nad 4/4 (oba downbeats 1+3 v 4/4 jsou taky
        #   downbeats v 2/4). Přijmeme 2/4 jen když contrast > 1.4× contrast 4/4.
        # - Podobně 6/8 vs 3/4.
        # Pokud rozdíl není přesvědčivý, padáme zpět na 4/4 (resp. 3/4) — častější
        # takt v klasické hudbě.
        best_label = max(contrasts, key=contrasts.get) if contrasts else "4/4"
        if best_label == "2/4" and "4/4" in contrasts:
            if contrasts["2/4"] < contrasts["4/4"] * 1.4:
                best_label = "4/4"
        if best_label == "6/8" and "3/4" in contrasts:
            # 6/8 vs 3/4 je těžší rozlišit — 6/8 = dva 3-beat trsy, struktura podobná.
            # Mírnější threshold (1.15×) než pro 2/4 vs 4/4.
            if contrasts["6/8"] < contrasts["3/4"] * 1.15:
                best_label = "3/4"
        return best_label, {k: round(v, 3) for k, v in contrasts.items()}
    except Exception as exc:
        logger.warning("Time signature detection selhala: %s, default 4/4", exc)
        return "4/4", {}
