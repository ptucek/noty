"""Correctness testy: porovnává ground-truth MusicXML vs výstup naší pipeline.

Pro každý fixture:
  1. Spustí transcribe.transcribe(wav, category, tmp) → list[NoteEvent]
  2. Spustí notation.events_to_musicxml(events, tmp) → MusicXML
  3. Načte ground-truth + transkript MusicXML přes music21
  4. Porovná posloupnost výšek not (s tolerancí na oktávu / enharmoniku)
  5. Reportuje % shody; test selže pod 70 %

Spuštění:
    cd <projekt> && uv run --extra dev pytest tests/test_correctness.py -v -s
nebo human-friendly report:
    cd <projekt> && uv run --extra dev python tests/test_correctness.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# Ensure src/ je na path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from music21 import converter  # noqa: E402
from music21 import note as m21_note  # noqa: E402

from transcribe_app.notation import events_to_musicxml  # noqa: E402
from transcribe_app.transcribe import transcribe  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MUTOPIA_DIR = FIXTURES_DIR / "mutopia"
MUTOPIA_INDEX = MUTOPIA_DIR / "INDEX.json"
REAL_DIR = FIXTURES_DIR / "real"
REAL_INDEX = REAL_DIR / "INDEX.json"

# Práh úspěšnosti – audio transkripce je nepřesná, akceptujeme 70 % shody
ACCEPT_THRESHOLD = 0.70

# Pokud je RUN_HEAVY_TESTS=0, přeskočíme drahé testy (Demucs/Basic Pitch)
HEAVY_DEFAULT = "1"
RUN_HEAVY = os.environ.get("RUN_HEAVY_TESTS", HEAVY_DEFAULT) != "0"

# Pokud je RUN_MUTOPIA_TESTS=0, přeskočíme Mutopia fixtures (jen syntetické 4 původní).
RUN_MUTOPIA = os.environ.get("RUN_MUTOPIA_TESTS", "1") != "0"

# Pokud je RUN_REAL_TESTS=0, přeskočíme Open Goldberg real fixtures.
RUN_REAL = os.environ.get("RUN_REAL_TESTS", "1") != "0"


@dataclass(frozen=True)
class Fixture:
    category: str
    name: str
    title: str
    # Explicitní cesty (přepisují defaultní layout fixtures/<cat>/<name>.{wav,musicxml}).
    # Mutopia fixtures mají vnořenou složku, takže potřebují override.
    wav_path: Path | None = field(default=None)
    xml_path: Path | None = field(default=None)

    @property
    def wav(self) -> Path:
        if self.wav_path is not None:
            return self.wav_path
        return FIXTURES_DIR / self.category / f"{self.name}.wav"

    @property
    def xml(self) -> Path:
        if self.xml_path is not None:
            return self.xml_path
        return FIXTURES_DIR / self.category / f"{self.name}.musicxml"


# Původní syntetické fixtures (zachováno – zpětně kompatibilní).
SYNTHETIC_FIXTURES: tuple[Fixture, ...] = (
    Fixture("monofonni", "ovcaci_ctveraci", "Ovčáci čtveráci"),
    Fixture("monofonni", "pec_nam_spadla", "Pec nám spadla"),
    Fixture("klavir", "bach_minuet_g_excerpt", "Bach – Menuet G dur (úvodní 4 takty)"),
    Fixture("vokal", "skakal_pes", "Skákal pes (vokál)"),
)


def _load_mutopia_fixtures() -> tuple[Fixture, ...]:
    """Načte Mutopia INDEX.json a vrátí list Fixture instancí.

    Pokud INDEX.json neexistuje (nikdo nespustil download.py), vrátí prázdný tuple.
    """
    if not MUTOPIA_INDEX.exists():
        return ()
    try:
        data = json.loads(MUTOPIA_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    out: list[Fixture] = []
    for p in data.get("pieces", []):
        try:
            wav = MUTOPIA_DIR / p["wav"]
            xml = MUTOPIA_DIR / p["musicxml"]
        except KeyError:
            continue
        if not wav.exists() or not xml.exists():
            continue
        out.append(
            Fixture(
                category=p["category"],
                name=f"mutopia_{p['slug']}",
                title=f"[Mutopia] {p.get('title', p['slug'])}",
                wav_path=wav,
                xml_path=xml,
            )
        )
    return tuple(out)


MUTOPIA_FIXTURES: tuple[Fixture, ...] = _load_mutopia_fixtures() if RUN_MUTOPIA else ()


def _load_real_fixtures() -> tuple[Fixture, ...]:
    """Načte Open Goldberg INDEX.json (real/) a vrátí Fixture instance.

    Ground truth pro tyto kusy pochází z engraved MuseScore zdroje (lossless
    konverze .mscz → MusicXML), na rozdíl od Mutopia (MIDI-derived).
    """
    if not REAL_INDEX.exists():
        return ()
    try:
        data = json.loads(REAL_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    out: list[Fixture] = []
    for p in data.get("pieces", []):
        try:
            wav = REAL_DIR / p["wav"]
            xml = REAL_DIR / p["musicxml"]
        except KeyError:
            continue
        if not wav.exists() or not xml.exists():
            continue
        out.append(
            Fixture(
                category=p.get("category", "klavir"),
                name=f"real_{p['slug']}",
                title=f"[OpenGoldberg] {p.get('title', p['slug'])}",
                wav_path=wav,
                xml_path=xml,
            )
        )
    return tuple(out)


REAL_FIXTURES: tuple[Fixture, ...] = _load_real_fixtures() if RUN_REAL else ()

# Všechny fixtures (synth + Mutopia + Open Goldberg). Pytest parametrize si je vyzobne.
FIXTURES: tuple[Fixture, ...] = SYNTHETIC_FIXTURES + MUTOPIA_FIXTURES + REAL_FIXTURES


# ---------- Pomocné funkce ----------

def _extract_pitch_classes(musicxml_path: Path) -> list[int]:
    """Vrátí seřazenou posloupnost pitch class (0-11) všech not, napříč party.

    Pro robustnost srovnání ignorujeme oktávu (pitch class) a chord rozkládáme
    na jednotlivé tóny v offsetu. Posloupnost je seřazena podle offsetu.
    """
    score = converter.parse(str(musicxml_path))
    events: list[tuple[float, int]] = []
    for n in score.recurse().notes:
        offset = float(n.getOffsetInHierarchy(score))
        if n.isChord:
            for p in n.pitches:
                events.append((offset, p.pitchClass))
        else:
            assert isinstance(n, m21_note.Note)
            events.append((offset, n.pitch.pitchClass))
    events.sort(key=lambda e: e[0])
    return [pc for _, pc in events]


def _extract_pitch_names(musicxml_path: Path) -> list[str]:
    """Lidsky čitelná posloupnost (C4, D#5, ...) podle offsetu, napříč party."""
    score = converter.parse(str(musicxml_path))
    events: list[tuple[float, str]] = []
    for n in score.recurse().notes:
        offset = float(n.getOffsetInHierarchy(score))
        if n.isChord:
            for p in n.pitches:
                events.append((offset, p.nameWithOctave))
        else:
            assert isinstance(n, m21_note.Note)
            events.append((offset, n.pitch.nameWithOctave))
    events.sort(key=lambda e: e[0])
    return [name for _, name in events]


def _lcs_match_ratio(gt: list[int], pred: list[int]) -> float:
    """Délka nejdelší společné podsekvence (pitch class) / délka GT.

    Robustní k chybějícím / extra notám: pokud pipeline přidá / vynechá noty,
    LCS pořád najde shodná místa. Ratio = 1.0 znamená všechny GT noty
    v správném pořadí přítomny v predikci.
    """
    if not gt or not pred:
        return 0.0
    m, n = len(gt), len(pred)
    # DP s úsporou paměti (jen 2 řádky)
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        cur = [0] * (n + 1)
        gi = gt[i - 1]
        for j in range(1, n + 1):
            if gi == pred[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                cur[j] = max(prev[j], cur[j - 1])
        prev = cur
    return prev[n] / len(gt)


def _key_signature_match(gt_xml: Path, pred_xml: Path) -> tuple[str, str, bool]:
    """Vrátí (gt_key_name, pred_key_name, match) – jednoduché porovnání tonality."""
    def get_key(path: Path) -> str:
        sc = converter.parse(str(path))
        try:
            k = sc.analyze("key")
            return f"{k.tonic.name} {k.mode}"
        except Exception:
            return "?"
    gk = get_key(gt_xml)
    pk = get_key(pred_xml)
    # Akceptujeme stejný tonic ignorujíc mode (často basic_pitch nemá kontext)
    gt_tonic = gk.split()[0] if gk != "?" else "?"
    pred_tonic = pk.split()[0] if pk != "?" else "?"
    return gk, pk, gt_tonic == pred_tonic


@dataclass
class CorrectnessReport:
    fixture: Fixture
    gt_pitches: list[str]
    pred_pitches: list[str]
    gt_pc: list[int]
    pred_pc: list[int]
    match_ratio: float
    gt_key: str
    pred_key: str
    key_match: bool
    pred_xml: Path
    error: str | None = None

    def pretty(self) -> str:
        lines = [
            "",
            "=" * 72,
            f"  {self.fixture.title}  [{self.fixture.category}]",
            "=" * 72,
        ]
        if self.error:
            lines.append(f"!! CHYBA: {self.error}")
            return "\n".join(lines)
        lines.append(
            f"  Ground-truth not: {len(self.gt_pitches)}   "
            f"transkripce not: {len(self.pred_pitches)}"
        )
        lines.append(
            f"  Shoda pitch-class (LCS): {self.match_ratio * 100:5.1f} %   "
            f"(práh {ACCEPT_THRESHOLD * 100:.0f} %)"
        )
        lines.append(
            f"  Tonalita: GT={self.gt_key}   PRED={self.pred_key}   "
            f"{'OK' if self.key_match else 'MISMATCH'}"
        )
        lines.append("")
        lines.append("  Side-by-side (prvních 32 not):")
        max_show = 32
        gt_show = self.gt_pitches[:max_show]
        pr_show = self.pred_pitches[:max_show]
        width = max(len(s) for s in gt_show + pr_show + ["GT"])
        lines.append(
            "    GT  : " + "  ".join(s.ljust(width) for s in gt_show)
        )
        lines.append(
            "    PRED: " + "  ".join(s.ljust(width) for s in pr_show)
        )
        lines.append(f"  Transkribovaný MusicXML: {self.pred_xml}")
        return "\n".join(lines)


def run_correctness(fixture: Fixture, workdir: Path) -> CorrectnessReport:
    """Spustí pipeline pro fixture a vrátí porovnávací report."""
    logging.getLogger().setLevel(logging.WARNING)
    try:
        result = transcribe(fixture.wav, fixture.category, workdir)  # type: ignore[arg-type]
        events = result.events
        pred_xml = events_to_musicxml(
            events, workdir, basename=fixture.name,
            bpm=result.tempo_bpm, key_sharps=result.key_sharps,
            time_signature=result.time_signature,
        )
    except Exception as exc:
        return CorrectnessReport(
            fixture=fixture,
            gt_pitches=[],
            pred_pitches=[],
            gt_pc=[],
            pred_pc=[],
            match_ratio=0.0,
            gt_key="?",
            pred_key="?",
            key_match=False,
            pred_xml=workdir / "(none)",
            error=f"pipeline selhala: {exc!r}",
        )

    gt_pc = _extract_pitch_classes(fixture.xml)
    pred_pc = _extract_pitch_classes(pred_xml)
    gt_names = _extract_pitch_names(fixture.xml)
    pred_names = _extract_pitch_names(pred_xml)
    ratio = _lcs_match_ratio(gt_pc, pred_pc)
    gk, pk, km = _key_signature_match(fixture.xml, pred_xml)

    return CorrectnessReport(
        fixture=fixture,
        gt_pitches=gt_names,
        pred_pitches=pred_names,
        gt_pc=gt_pc,
        pred_pc=pred_pc,
        match_ratio=ratio,
        gt_key=gk,
        pred_key=pk,
        key_match=km,
        pred_xml=pred_xml,
    )


# ---------- pytest testy ----------

@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_fixture_exists(fixture: Fixture) -> None:
    assert fixture.wav.exists(), f"Chybí audio fixture: {fixture.wav}"
    assert fixture.xml.exists(), f"Chybí ground-truth MusicXML: {fixture.xml}"


def _vocal_separator_available() -> bool:
    """RoFormer (audio-separator) NEBO Demucs musí být importovatelný."""
    try:
        import audio_separator.separator  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import demucs.apply  # noqa: F401
        import demucs.pretrained  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import demucs.api  # noqa: F401
        return True
    except Exception:
        return False


@pytest.mark.skipif(not RUN_HEAVY, reason="RUN_HEAVY_TESTS=0")
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_pipeline_correctness(fixture: Fixture, tmp_path: Path, capsys) -> None:
    if fixture.category == "vokal" and not _vocal_separator_available():
        pytest.skip("Žádný vokál separator (RoFormer/Demucs) není dostupný")
    report = run_correctness(fixture, tmp_path)
    with capsys.disabled():
        print(report.pretty())
    if report.error:
        pytest.fail(report.error)
    assert report.match_ratio >= ACCEPT_THRESHOLD, (
        f"{fixture.title}: shoda {report.match_ratio * 100:.1f} % "
        f"< práh {ACCEPT_THRESHOLD * 100:.0f} %"
    )


# ---------- Human-friendly CLI ----------

def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    tmpdir = Path(tempfile.mkdtemp(prefix="noty_correctness_"))
    print(f"Workdir: {tmpdir}\n")

    reports = []
    for fx in FIXTURES:
        if not fx.wav.exists() or not fx.xml.exists():
            print(f"!! Chybí fixture {fx.name}, spusť tests/fixtures/generate_fixtures.py")
            continue
        per_dir = tmpdir / fx.name
        per_dir.mkdir(parents=True, exist_ok=True)
        rep = run_correctness(fx, per_dir)
        reports.append(rep)
        print(rep.pretty())

    print("\n" + "=" * 72)
    print("  SHRNUTÍ")
    print("=" * 72)
    for r in reports:
        status = (
            "ERR" if r.error
            else "OK " if r.match_ratio >= ACCEPT_THRESHOLD
            else "LOW"
        )
        print(
            f"  [{status}] {r.fixture.category:<10s} {r.fixture.name:<28s} "
            f"shoda {r.match_ratio * 100:5.1f} %   GT={len(r.gt_pitches):>3d} "
            f"PRED={len(r.pred_pitches):>3d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
