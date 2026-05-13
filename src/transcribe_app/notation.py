"""Note events → MusicXML. Postaví čistý music21 score na 16th-note gridu, detekuje tóninu a rozdělí na bass/treble."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from music21 import chord, clef, instrument, key as m21key, meter, note, stream, tempo

logger = logging.getLogger(__name__)

DEFAULT_BPM = 120
GRID_DIVISIONS = 4
MIN_DURATION_QL = 0.25
MAX_DURATION_QL = 16.0
SPLIT_PITCH_MIDI = 60  # C4 — split point pro bass vs treble klíč


@dataclass(frozen=True)
class NoteEvent:
    pitch_midi: int
    start_s: float
    end_s: float
    velocity: int = 80


def events_to_musicxml(
    events: list[NoteEvent],
    output_dir: Path,
    basename: str = "transcription",
    bpm: float = DEFAULT_BPM,
    multi_staff: bool = True,
    key_sharps: int | None = None,
    time_signature: str = "4/4",
) -> Path:
    """Postaví čistý music21 score z note eventů, případně rozdělí na bass+treble. Tónina z parametru nebo z analýzy."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{basename}.musicxml"

    score = stream.Score()

    seconds_per_quarter = 60.0 / bpm
    raw_end_ql = max((e.end_s / seconds_per_quarter for e in events), default=4.0)
    measure_ql = _measure_length_ql(time_signature)
    total_ql = max(measure_ql, _ceil_to_measure(raw_end_ql, measure_ql))

    if multi_staff and _should_split_staves(events):
        treble_events = [e for e in events if e.pitch_midi >= SPLIT_PITCH_MIDI]
        bass_events = [e for e in events if e.pitch_midi < SPLIT_PITCH_MIDI]
        treble_part = _build_part(
            treble_events, bpm, "Pravá ruka", clef.TrebleClef(), instrument.Piano(), total_ql, time_signature
        )
        bass_part = _build_part(
            bass_events, bpm, "Levá ruka", clef.BassClef(), instrument.Piano(), total_ql, time_signature
        )
        score.insert(0, treble_part)
        score.insert(0, bass_part)
        logger.info("Multi-staff: treble %d not, bass %d not, %s, %.1f QL", len(treble_events), len(bass_events), time_signature, total_ql)
    else:
        part = _build_part(events, bpm, "Part 1", clef.TrebleClef(), instrument.Piano(), total_ql, time_signature)
        score.insert(0, part)

    if key_sharps is not None:
        _insert_key_signature(score, key_sharps)
    else:
        _apply_detected_key(score)

    score.write("musicxml", fp=str(output_path))
    logger.info(
        "MusicXML zapsán: %s (%d not @ %.0f BPM, %d bytes)",
        output_path, len(events), bpm, output_path.stat().st_size,
    )
    return output_path


def _build_part(
    events: list[NoteEvent],
    bpm: float,
    name: str,
    clef_type: clef.Clef,
    inst: instrument.Instrument,
    total_ql: float,
    time_signature: str = "4/4",
) -> stream.Part:
    """Postaví jednu osnovu z note eventů, snapnuto na 16th-note grid, vyplní na total_ql."""
    part = stream.Part()
    part.id = name
    part.partName = name
    part.insert(0, inst)
    part.insert(0, tempo.MetronomeMark(number=int(round(bpm))))
    part.insert(0, meter.TimeSignature(time_signature))
    part.insert(0, clef_type)

    seconds_per_quarter = 60.0 / bpm
    grid_ql = 1.0 / GRID_DIVISIONS
    buckets = _bucket_by_onset(events, window_s=0.05)

    last_end_ql = 0.0
    for bucket in buckets:
        start_ql = _snap(bucket[0].start_s / seconds_per_quarter, grid_ql)
        end_ql = _snap(max(e.end_s for e in bucket) / seconds_per_quarter, grid_ql)
        dur_ql = max(MIN_DURATION_QL, min(MAX_DURATION_QL, end_ql - start_ql))
        if start_ql < last_end_ql:
            start_ql = last_end_ql
        # Truncate so that start_ql + dur_ql nikdy nepřesáhne total_ql.
        if start_ql >= total_ql:
            break
        dur_ql = min(dur_ql, total_ql - start_ql)
        if dur_ql < MIN_DURATION_QL:
            continue
        gap = start_ql - last_end_ql
        if gap >= MIN_DURATION_QL:
            part.append(note.Rest(quarterLength=_round_ql(gap)))
        pitches = sorted({e.pitch_midi for e in bucket})
        if len(pitches) == 1:
            elem: note.Note | chord.Chord = note.Note(pitches[0], quarterLength=_round_ql(dur_ql))
        else:
            elem = chord.Chord(pitches, quarterLength=_round_ql(dur_ql))
        elem.volume.velocity = max(e.velocity for e in bucket)
        part.append(elem)
        last_end_ql = start_ql + dur_ql

    # Pad to total_ql ⇒ obě osnovy mají stejnou délku (MuseScore vyžaduje).
    if last_end_ql < total_ql:
        part.append(note.Rest(quarterLength=total_ql - last_end_ql))

    part.makeMeasures(inPlace=True)
    return part


def _bucket_by_onset(events: list[NoteEvent], window_s: float = 0.05) -> list[list[NoteEvent]]:
    """Seskupí eventy se start_s do window_s do akordů. Vrátí list 'bucketů'."""
    if not events:
        return []
    sorted_ev = sorted(events, key=lambda e: e.start_s)
    buckets: list[list[NoteEvent]] = [[sorted_ev[0]]]
    for ev in sorted_ev[1:]:
        if ev.start_s - buckets[-1][0].start_s < window_s:
            buckets[-1].append(ev)
        else:
            buckets.append([ev])
    return buckets


def _ceil_to_measure(ql: float, measure_ql: float) -> float:
    """Zaokrouhlí ql nahoru na nejbližší násobek measure_ql (např. 4.0 pro 4/4)."""
    import math
    return math.ceil(ql / measure_ql) * measure_ql


def _measure_length_ql(time_signature: str) -> float:
    """'3/4' → 3.0, '4/4' → 4.0, '6/8' → 3.0."""
    try:
        num, den = time_signature.split("/")
        return float(num) * (4.0 / float(den))
    except Exception:
        return 4.0


def _should_split_staves(events: list[NoteEvent]) -> bool:
    """Rozdělit na bass+treble, když je v audiu výrazný rozsah překračující C4 oběma směry."""
    if len(events) < 10:
        return False
    below = sum(1 for e in events if e.pitch_midi < SPLIT_PITCH_MIDI)
    above = sum(1 for e in events if e.pitch_midi >= SPLIT_PITCH_MIDI)
    return below >= 3 and above >= 3


def _insert_key_signature(score: stream.Score, sharps: int) -> None:
    """Vloží KeySignature (počet ♯/♭) do prvního taktu každé osnovy."""
    for part in score.parts:
        measures = list(part.getElementsByClass(stream.Measure))
        if measures:
            measures[0].insert(0, m21key.KeySignature(sharps))
        else:
            part.insert(0, m21key.KeySignature(sharps))


def _snap(value: float, grid: float) -> float:
    return round(value / grid) * grid


def _round_ql(ql: float) -> float:
    snapped = round(ql / 0.25) * 0.25
    return max(0.25, snapped)
