"""Generuje testovací fixtures (MusicXML ground truth + syntetické WAV audio).

Spuštění:
    cd <projekt> && uv run python tests/fixtures/generate_fixtures.py

Všechny melodie jsou v public domain (české lidové písně, J.S. Bach – Notenbüchlein
für Anna Magdalena Bach, BWV Anh. 114). Audio je synthetické (součet sinusoid
s krátkým fade-in/out obálkou) – zajišťuje čistý signál pro test pipeline.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from music21 import (
    clef,
    instrument,
    key,
    meter,
    note,
    stream,
    tempo,
)

FIXTURES_DIR = Path(__file__).resolve().parent
SR = 22050  # vzorkovací frekvence (Hz)


# ---------- Audio syntéza ----------

def _adsr_envelope(n_samples: int, sr: int) -> np.ndarray:
    """Krátké lineární fade-in/out (~20 ms) aby tóny neklikaly."""
    env = np.ones(n_samples, dtype=np.float32)
    fade = min(int(0.02 * sr), n_samples // 4)
    if fade > 0:
        env[:fade] = np.linspace(0.0, 1.0, fade)
        env[-fade:] = np.linspace(1.0, 0.0, fade)
    return env


def _sine_note(freq: float, duration_s: float, sr: int, harmonics: tuple[float, ...] = (1.0, 0.3, 0.15)) -> np.ndarray:
    """Tón = součet sinusoid harmonických násobků f0 s ADSR obálkou."""
    n = int(sr * duration_s)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n) / sr
    wave = np.zeros(n, dtype=np.float32)
    for k, amp in enumerate(harmonics, start=1):
        wave += amp * np.sin(2 * np.pi * freq * k * t).astype(np.float32)
    wave /= max(1e-9, np.max(np.abs(wave)))
    return 0.5 * wave * _adsr_envelope(n, sr)


def _vowel_note(freq: float, duration_s: float, sr: int) -> np.ndarray:
    """Vokálně znějící tón – součet formantů aby simuloval lidský hlas (otevřené 'a').

    Pro vocal kategorii je důležitý spektrální obsah; používáme f0 + formanty F1, F2."""
    n = int(sr * duration_s)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n) / sr
    # formanty pro samohlásku 'a' (~700 Hz, ~1100 Hz)
    f1, f2, f3 = 700.0, 1100.0, 2600.0

    def harm_stack(base: float) -> np.ndarray:
        s = np.zeros(n, dtype=np.float32)
        for k in range(1, 12):
            s += (1.0 / k) * np.sin(2 * np.pi * base * k * t).astype(np.float32)
        return s

    src = harm_stack(freq)

    # jednoduché peakování na formantech: konvoluce s krátkým sinem nebudeme dělat,
    # místo toho jen přimícháme formant-modulované sinusoidy
    fmt = (
        0.6 * np.sin(2 * np.pi * f1 * t).astype(np.float32) * src
        + 0.3 * np.sin(2 * np.pi * f2 * t).astype(np.float32) * src
        + 0.1 * np.sin(2 * np.pi * f3 * t).astype(np.float32) * src
    )
    wave = src + 0.3 * fmt
    wave /= max(1e-9, np.max(np.abs(wave)))
    # mírná vibrato (5 Hz, ±0.5%)
    vib = 1.0 + 0.005 * np.sin(2 * np.pi * 5.0 * t)
    wave = wave * vib.astype(np.float32)
    return 0.45 * wave * _adsr_envelope(n, sr)


def synthesize_stream(
    s: stream.Stream,
    sr: int = SR,
    bpm: float | None = None,
    timbre: str = "sine",
) -> np.ndarray:
    """Převede music21 stream na mono numpy audio.

    Pro polyfonii: časově řadí podle offsetu, mixuje paralelní noty.
    """
    if bpm is None:
        mm = s.recurse().getElementsByClass(tempo.MetronomeMark)
        bpm = mm[0].number if len(mm) else 120.0
    sec_per_quarter = 60.0 / bpm

    notes_with_times = []
    for n in s.recurse().notes:
        offset_s = float(n.getOffsetInHierarchy(s)) * sec_per_quarter
        dur_s = float(n.duration.quarterLength) * sec_per_quarter
        if n.isChord:
            for p in n.pitches:
                notes_with_times.append((offset_s, dur_s, p.frequency))
        else:
            notes_with_times.append((offset_s, dur_s, n.pitch.frequency))

    if not notes_with_times:
        return np.zeros(int(sr * 1.0), dtype=np.float32)

    total_dur = max(o + d for o, d, _ in notes_with_times) + 0.3
    audio = np.zeros(int(sr * total_dur) + 1, dtype=np.float32)

    synth_fn = _vowel_note if timbre == "vowel" else _sine_note
    for offset_s, dur_s, freq in notes_with_times:
        wave = synth_fn(freq, dur_s, sr)
        start = int(offset_s * sr)
        end = start + len(wave)
        if end > len(audio):
            audio = np.pad(audio, (0, end - len(audio)))
        audio[start:end] += wave

    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = 0.85 * audio / peak
    return audio.astype(np.float32)


# ---------- Melodie ----------

def make_ovcaci_ctveraci() -> stream.Score:
    """Ovčáci, čtveráci – česká lidová (public domain).

    Tradiční nápěv v C dur, 4/4, krátká fráze 8 not.
    Text: "Ovčáci, čtveráci, počkejte mě, vovce mi tam zajdou do zelený jetelí"
    Tady kódujeme úvodní motiv: G G E E F F D D (jednoduchá verze).
    """
    sc = stream.Score()
    p = stream.Part()
    p.partName = "Melody"
    p.insert(0, instrument.Flute())
    p.append(clef.TrebleClef())
    p.append(key.KeySignature(0))  # C major
    p.append(meter.TimeSignature("4/4"))
    p.append(tempo.MetronomeMark(number=110))

    pitches = ["G4", "G4", "E4", "E4", "F4", "F4", "D4", "D4", "E4", "F4", "G4", "G4", "C4"]
    durations = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2]
    for pn, d in zip(pitches, durations):
        p.append(note.Note(pn, quarterLength=d))
    sc.append(p)
    return sc


def make_pec_nam_spadla() -> stream.Score:
    """Pec nám spadla – česká lidová (public domain).

    Velmi jednoduchá fráze v C dur, monofonní.
    Klasický motiv: C C D E E D C  (pec / nám / spad-la / kdo-pak / nám / ji / po-sta-ví)
    """
    sc = stream.Score()
    p = stream.Part()
    p.partName = "Melody"
    p.insert(0, instrument.Recorder())
    p.append(clef.TrebleClef())
    p.append(key.KeySignature(0))
    p.append(meter.TimeSignature("4/4"))
    p.append(tempo.MetronomeMark(number=100))

    pitches = ["C5", "C5", "D5", "E5", "E5", "D5", "C5", "C5", "D5", "E5", "C5"]
    durations = [1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 2]
    for pn, d in zip(pitches, durations):
        p.append(note.Note(pn, quarterLength=d))
    sc.append(p)
    return sc


def make_bach_minuet_g_excerpt() -> stream.Score:
    """Menuet G dur – BWV Anh. 114 (z Notenbüchlein für Anna Magdalena Bach 1725).

    Autorství dnes přisuzováno Christianu Petzoldovi (zemř. 1733) – pro PD účely
    irelevantní, melodie je >300 let stará. Kódujeme úvodní 4 takty pravé ruky
    + jednoduchý basový doprovod levé ruky – polyfonní piano test.
    """
    sc = stream.Score()
    rh = stream.Part()
    rh.partName = "RightHand"
    rh.insert(0, instrument.Piano())
    rh.append(clef.TrebleClef())
    rh.append(key.KeySignature(1))  # G major (1 sharp)
    rh.append(meter.TimeSignature("3/4"))
    rh.append(tempo.MetronomeMark(number=110))

    # Úvodní 4 takty pravé ruky (zjednodušeno)
    # T1: D5 (q) | G4 A4 B4 C5 (8th)  → 1 + 0.5*4 = 3 ✓
    # T2: D5 (q) | G4 (q) | G4 (q)
    # T3: E5 (q) | C5 D5 E5 F#5 (8th)
    # T4: G5 (q) | G4 (q) | rest (q)
    rh_notes: list[tuple[str, float]] = [
        ("D5", 1.0), ("G4", 0.5), ("A4", 0.5), ("B4", 0.5), ("C5", 0.5),
        ("D5", 1.0), ("G4", 1.0), ("G4", 1.0),
        ("E5", 1.0), ("C5", 0.5), ("D5", 0.5), ("E5", 0.5), ("F#5", 0.5),
        ("G5", 1.0), ("G4", 1.0), ("G4", 1.0),
    ]
    for pn, d in rh_notes:
        rh.append(note.Note(pn, quarterLength=d))

    lh = stream.Part()
    lh.partName = "LeftHand"
    lh.insert(0, instrument.Piano())
    lh.append(clef.BassClef())
    lh.append(key.KeySignature(1))
    lh.append(meter.TimeSignature("3/4"))
    lh.append(tempo.MetronomeMark(number=110))

    # Velmi jednoduchý bas (3 čtvrtky/takt)
    lh_notes: list[tuple[str, float]] = [
        ("G2", 1.0), ("B2", 1.0), ("D3", 1.0),
        ("G2", 1.0), ("B2", 1.0), ("D3", 1.0),
        ("C3", 1.0), ("E3", 1.0), ("G3", 1.0),
        ("D3", 1.0), ("G2", 1.0), ("G2", 1.0),
    ]
    for pn, d in lh_notes:
        lh.append(note.Note(pn, quarterLength=d))

    sc.append(rh)
    sc.append(lh)
    return sc


def make_skakal_pes_vocal() -> stream.Score:
    """Skákal pes – česká lidová (public domain).

    Monofonní vokální linka pro test vokal kategorie. Pipeline vokal nejdřív
    Demucs izoluje vokál; protože náš syntetický signál JE vokál (formantový),
    Demucs ho prostě nechá projít a Basic Pitch by ho měl přepsat.
    """
    sc = stream.Score()
    p = stream.Part()
    p.partName = "Vocal"
    p.insert(0, instrument.Vocalist())
    p.append(clef.TrebleClef())
    p.append(key.KeySignature(0))
    p.append(meter.TimeSignature("2/4"))
    p.append(tempo.MetronomeMark(number=120))

    # Skákal pes přes oves, přes zelenou louku, šel za ním myslivec...
    # Zjednodušená forma první fráze:
    # G G G E | A A A F | G F E D | C
    pitches = ["G4", "G4", "G4", "E4", "A4", "A4", "A4", "F4",
               "G4", "F4", "E4", "D4", "C4"]
    durations = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5,
                 0.5, 0.5, 0.5, 0.5, 1.0]
    for pn, d in zip(pitches, durations):
        p.append(note.Note(pn, quarterLength=d))
    sc.append(p)
    return sc


# ---------- Zápis fixtures ----------

FIXTURES: list[dict] = [
    {
        "category": "monofonni",
        "name": "ovcaci_ctveraci",
        "title": "Ovčáci čtveráci",
        "source": "Česká lidová píseň (public domain)",
        "license": "Public Domain (traditional Czech folk)",
        "make": make_ovcaci_ctveraci,
        "timbre": "sine",
        "notes_human": "G G E E F F D D E F G G C  (13 not, C dur, 4/4)",
    },
    {
        "category": "monofonni",
        "name": "pec_nam_spadla",
        "title": "Pec nám spadla",
        "source": "Česká lidová píseň (public domain)",
        "license": "Public Domain (traditional Czech folk)",
        "make": make_pec_nam_spadla,
        "timbre": "sine",
        "notes_human": "C C D E E D C C D E C  (~11 not, C dur, 4/4 – music21 může rozdělit dlouhou notu pomlčkou na 2 takty)",
    },
    {
        "category": "klavir",
        "name": "bach_minuet_g_excerpt",
        "title": "Menuet G dur (BWV Anh. 114) – úvodní 4 takty",
        "source": "J.S. Bach (atrib.) / Christian Petzold, Notenbüchlein für Anna Magdalena Bach 1725",
        "license": "Public Domain (composer died 1733/1750)",
        "make": make_bach_minuet_g_excerpt,
        "timbre": "sine",
        "notes_human": (
            "Pravá ruka: D5 G4 A4 B4 C5 D5 G4 G4 E5 C5 D5 E5 F#5 G5 G4 G4 (16 not, G dur, 3/4)\n"
            "Levá ruka:  G2 B2 D3 G2 B2 D3 C3 E3 G3 D3 G2 G2 (12 not)"
        ),
    },
    {
        "category": "vokal",
        "name": "skakal_pes",
        "title": "Skákal pes",
        "source": "Česká lidová píseň (public domain)",
        "license": "Public Domain (traditional Czech folk)",
        "make": make_skakal_pes_vocal,
        "timbre": "vowel",
        "notes_human": "G G G E A A A F G F E D C  (13 not, C dur, 2/4)",
    },
]


def write_fixture(fx: dict) -> dict:
    """Zapíše musicxml + wav + notes.md pro jeden fixture. Vrací dict info."""
    out_dir = FIXTURES_DIR / fx["category"]
    out_dir.mkdir(parents=True, exist_ok=True)

    sc = fx["make"]()
    xml_path = out_dir / f"{fx['name']}.musicxml"
    sc.write("musicxml", fp=str(xml_path))

    audio = synthesize_stream(sc, sr=SR, timbre=fx["timbre"])
    wav_path = out_dir / f"{fx['name']}.wav"
    sf.write(str(wav_path), audio, SR, subtype="PCM_16")

    notes_md = out_dir / f"{fx['name']}.notes.md"
    notes_md.write_text(
        f"# {fx['title']}\n\n"
        f"- **Kategorie:** {fx['category']}\n"
        f"- **Zdroj:** {fx['source']}\n"
        f"- **Licence:** {fx['license']}\n"
        f"- **Audio timbre:** {fx['timbre']}\n"
        f"- **Audio délka:** {len(audio) / SR:.2f} s @ {SR} Hz\n\n"
        f"## Noty (lidsky čitelně)\n\n{fx['notes_human']}\n\n"
        f"## Soubory\n\n"
        f"- `{fx['name']}.musicxml` – ground-truth notace (otevři v MuseScore 4)\n"
        f"- `{fx['name']}.wav` – syntetické audio (vstup pro pipeline)\n",
        encoding="utf-8",
    )
    return {
        "category": fx["category"],
        "name": fx["name"],
        "xml": xml_path,
        "wav": wav_path,
        "notes": notes_md,
        "duration_s": len(audio) / SR,
        "wav_size_kb": wav_path.stat().st_size / 1024,
    }


def main() -> None:
    print(f"Generuji fixtures do {FIXTURES_DIR}")
    results = [write_fixture(fx) for fx in FIXTURES]
    print("\nHotovo:")
    for r in results:
        print(
            f"  [{r['category']:>10s}] {r['name']:<28s} "
            f"{r['duration_s']:.2f}s  {r['wav_size_kb']:.0f} kB"
        )


if __name__ == "__main__":
    main()
