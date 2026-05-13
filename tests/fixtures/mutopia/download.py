"""Stáhne kurátorovaný dataset MIDI z Mutopia Project, vyrobí WAV + MusicXML páry.

Pipeline pro každý piece:
  1. Stáhne piece-info.cgi?id=<id> → parsuje MIDI URL, název, autora, licenci
  2. Stáhne MIDI
  3. Načte do music21, zkrátí na prvních N taktů (cíl ≤30 s při daném tempu)
  4. Exportuje MusicXML (ground truth)
  5. Exportuje truncated MIDI a převede přes MuseScore CLI na WAV
  6. Převede stereo float WAV → mono 16-bit PCM (úspora místa)
  7. Zapíše per-piece JSON s metadaty a aktualizuje INDEX.json

Spuštění:
    cd <projekt> && uv run python tests/fixtures/mutopia/download.py

Skript je idempotentní – už stažené pieces přeskočí.
Mezi HTTP requesty čeká 1 s (respekt k Mutopia serveru).

Licenční filtr: přijímáme jen `Public Domain` (CC0), `CC BY 3.0`, `CC BY 4.0`.
CC-BY-SA pieces jsou explicitně vyloučeny dle zadání.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# music21 a soundfile – součást projektových dependencí
from music21 import converter, stream, tempo as m21_tempo  # noqa: E402
import soundfile as sf  # noqa: E402
import numpy as np  # noqa: E402

logger = logging.getLogger("mutopia.download")

MUSESCORE = "/Applications/MuseScore 4.app/Contents/MacOS/mscore"
PIECE_INFO_URL = "https://www.mutopiaproject.org/cgibin/piece-info.cgi?id={id}"
USER_AGENT = "noty-fixtures/1.0 (Czech ZUS audio-to-sheet-music tests)"

# Maximální cílená délka rendrovaného WAV (sec)
MAX_DURATION_S = 30.0
# Při neznámém tempu předpokládáme tento BPM
FALLBACK_BPM = 100.0

# Akceptované licence – musí obsahovat jedno z těchto klíčových slov v licenci.
# Filtrujeme NAHORU – pokud je v licenci "ShareAlike", zamítáme.
LICENSE_ACCEPT_PATTERNS = (
    re.compile(r"public\s*domain", re.I),
    re.compile(r"no rights reserved", re.I),
    re.compile(r"CC0", re.I),
    re.compile(r"creative commons attribution\b(?!.*share[-\s]?alike)", re.I),
)
LICENSE_REJECT_PATTERNS = (
    re.compile(r"share[-\s]?alike", re.I),
    re.compile(r"CC[-\s]?BY[-\s]?SA", re.I),
    re.compile(r"non[-\s]?commercial", re.I),
)


@dataclass
class Candidate:
    """Vstupní záznam: Mutopia ID + slug + category + počet taktů k zachování."""

    mutopia_id: int
    slug: str
    category: str  # monofonni | klavir | kapela
    measures: int = 8  # počet taktů, kolik si od piece nechat


# Kurátorovaný seznam ~45 kandidátů. Vybráno z piece-info procházení Mutopia.
# Licence všech těchto byla na piece-info stránce uvedena jako "Public Domain"
# (CC: No rights reserved) – při downloadu se ověřuje znovu.
CANDIDATES: tuple[Candidate, ...] = (
    # ---- monofonni (sólové nástroje, melodické linky) ----
    Candidate(1014, "bach_minuet_bflat", "monofonni", measures=8),
    Candidate(1612, "bach_minuet_a_minor", "monofonni", measures=8),
    Candidate(1613, "bach_minuet_c_minor", "monofonni", measures=8),
    Candidate(1013, "bach_polonaise_f", "monofonni", measures=8),
    Candidate(905, "good_king_wenceslas", "monofonni", measures=12),
    Candidate(1630, "duke_of_norfolk", "monofonni", measures=12),
    Candidate(194, "old_100th", "monofonni", measures=16),
    Candidate(197, "winchester_new", "monofonni", measures=16),
    Candidate(151, "handel_gavotte_aylesford", "monofonni", measures=8),
    Candidate(1535, "bach_siciliano", "monofonni", measures=8),
    Candidate(1534, "bach_air_g_string", "monofonni", measures=6),
    Candidate(525, "leoni_satb_excerpt", "monofonni", measures=12),
    # ---- klavir (klavírní krátké kusy / úvodní takty) ----
    Candidate(614, "mozart_kv331_tema", "klavir", measures=8),
    Candidate(241, "mozart_sonata_c_fragment", "klavir", measures=8),
    Candidate(447, "mozart_fugue_375g", "klavir", measures=8),
    Candidate(424, "mozart_fugue_kv153", "klavir", measures=8),
    Candidate(425, "mozart_fugue_kv154", "klavir", measures=8),
    Candidate(470, "chopin_prelude_op28_7", "klavir", measures=16),
    Candidate(469, "chopin_prelude_op28_6", "klavir", measures=12),
    Candidate(471, "chopin_prelude_op28_15", "klavir", measures=8),
    Candidate(472, "chopin_prelude_op28_20", "klavir", measures=12),
    Candidate(468, "chopin_prelude_op28_4", "klavir", measures=12),
    Candidate(921, "chopin_prelude_op28_4_peters", "klavir", measures=12),
    Candidate(1776, "chopin_prelude_op45", "klavir", measures=8),
    Candidate(354, "schumann_kinderszenen_1", "klavir", measures=8),
    Candidate(372, "schumann_kinderszenen_5", "klavir", measures=8),
    Candidate(504, "schumann_kinderszenen_7", "klavir", measures=8),
    Candidate(931, "beethoven_fur_elise", "klavir", measures=8),
    Candidate(939, "beethoven_prelude_woo55", "klavir", measures=8),
    Candidate(804, "clementi_sonatina_op36", "klavir", measures=8),
    Candidate(1211, "beethoven_sonata1_mov1", "klavir", measures=8),
    Candidate(1276, "beethoven_sonata1_mov3", "klavir", measures=8),
    Candidate(1277, "beethoven_sonata1_mov4", "klavir", measures=8),
    Candidate(992, "beethoven_sonata6_mov1", "klavir", measures=8),
    Candidate(993, "beethoven_sonata6_mov2", "klavir", measures=8),
    Candidate(994, "beethoven_sonata6_mov3", "klavir", measures=8),
    Candidate(314, "schumann_romanze_op28_2", "klavir", measures=8),
    Candidate(378, "bach_chorale_aus_meines_herzens", "klavir", measures=8),
    Candidate(615, "mozart_kv331_var1", "klavir", measures=8),
    Candidate(2106, "bach_bwv454", "klavir", measures=8),
    # ---- kapela / multi-instrument (chamber music) ----
    Candidate(790, "beethoven_quartet4_mov1", "kapela", measures=6),
    Candidate(791, "beethoven_quartet4_mov2", "kapela", measures=6),
    Candidate(792, "beethoven_quartet4_mov3", "kapela", measures=8),
    Candidate(793, "beethoven_quartet4_mov4", "kapela", measures=8),
    Candidate(298, "corelli_christmas_concerto", "kapela", measures=6),
    Candidate(342, "donizetti_quartet18", "kapela", measures=6),
    Candidate(1197, "beethoven_trio_op11_mov1", "kapela", measures=6),
)


# ---------- HTTP / parsing ----------

def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


_TITLE_RE = re.compile(r"<title>([^<]+?)</title>", re.I)
_H2_RE = re.compile(r"<h2>([^<]+?)</h2>", re.I)
_H4_RE = re.compile(r"<h4>([^<]+?)</h4>", re.I)
_MID_URL_RE = re.compile(r'https?://[^\s"\']+\.mid\b', re.I)
_LICENSE_IMG_RE = re.compile(r'alt="(CC[^"]+?)"', re.I)
_LICENSE_TEXT_RE = re.compile(r"(public domain|creative commons[^<]+)", re.I)


@dataclass
class MutopiaMeta:
    mutopia_id: int
    title: str
    composer: str
    license: str
    source_url: str
    mid_url: str


def fetch_piece_meta(mutopia_id: int) -> MutopiaMeta:
    """Stáhne piece-info.cgi a vyextrahuje metadata + MIDI URL."""
    url = PIECE_INFO_URL.format(id=mutopia_id)
    html = _http_get(url).decode("utf-8", errors="replace")
    # Title
    title = ""
    h2 = _H2_RE.search(html)
    if h2:
        title = h2.group(1).strip()
    elif (t := _TITLE_RE.search(html)):
        title = t.group(1).strip()
    # Composer
    composer = ""
    h4 = _H4_RE.search(html)
    if h4:
        composer = h4.group(1).strip().lstrip("by").strip()
    # MIDI URL
    mid_match = _MID_URL_RE.search(html)
    if not mid_match:
        raise RuntimeError(f"piece {mutopia_id}: MIDI URL not found")
    mid_url = mid_match.group(0)
    # License – nejprve textově, pak fallback na alt z CC obrázku
    lic = ""
    if (lt := _LICENSE_TEXT_RE.search(html)):
        lic = lt.group(1).strip()
    elif (li := _LICENSE_IMG_RE.search(html)):
        lic = li.group(1).strip()
    if not lic:
        lic = "unknown"
    return MutopiaMeta(
        mutopia_id=mutopia_id,
        title=title,
        composer=composer,
        license=lic,
        source_url=url,
        mid_url=mid_url,
    )


def license_is_acceptable(license_text: str) -> bool:
    if any(p.search(license_text) for p in LICENSE_REJECT_PATTERNS):
        return False
    return any(p.search(license_text) for p in LICENSE_ACCEPT_PATTERNS)


# ---------- MIDI truncation / WAV/MusicXML rendering ----------

def truncate_midi(
    src_midi: Path,
    dst_midi: Path,
    dst_musicxml: Path,
    measures: int,
) -> dict[str, Any]:
    """Načte MIDI, ořeže na prvních N taktů, zapíše MIDI + MusicXML.

    Vrátí metadata (bpm, čas, počet not, time signature, key).
    """
    sc = converter.parse(str(src_midi))
    total_measures = max(
        (len(list(p.getElementsByClass("Measure"))) for p in sc.parts),
        default=0,
    )
    if total_measures == 0:
        # Některá MIDI nemají měření – nech jak je.
        truncated: stream.Score = sc  # type: ignore[assignment]
    else:
        n = min(measures, total_measures)
        truncated = sc.measures(1, n)  # type: ignore[assignment]

    # Tempo
    bpm = FALLBACK_BPM
    mm_list = list(truncated.recurse().getElementsByClass("MetronomeMark"))
    if not mm_list:
        mm_list = list(sc.recurse().getElementsByClass("MetronomeMark"))
    if mm_list:
        try:
            bpm = float(mm_list[0].getQuarterBPM())
        except Exception:
            try:
                bpm = float(mm_list[0].number)  # type: ignore[union-attr]
            except Exception:
                bpm = FALLBACK_BPM
    # Pokud truncated nemá MetronomeMark, vlož ho ručně (jinak music21 default 120).
    if not list(truncated.recurse().getElementsByClass("MetronomeMark")):
        # Vlož na začátek prvního partu
        try:
            first_part = truncated.parts[0]
            first_part.insert(0, m21_tempo.MetronomeMark(number=bpm))
        except Exception:
            pass

    ql = float(truncated.duration.quarterLength)
    duration_s = ql * 60.0 / bpm if bpm > 0 else 0.0

    # Time signature + key
    ts = ""
    try:
        ts_obj = next(iter(truncated.recurse().getElementsByClass("TimeSignature")), None)
        if ts_obj is not None:
            ts = f"{ts_obj.numerator}/{ts_obj.denominator}"
    except Exception:
        pass

    key_name = ""
    try:
        k = truncated.analyze("key")
        key_name = f"{k.tonic.name} {k.mode}"
    except Exception:
        pass

    notes_count = sum(1 for _ in truncated.recurse().notes)

    dst_midi.parent.mkdir(parents=True, exist_ok=True)
    truncated.write("midi", fp=str(dst_midi))
    truncated.write("musicxml", fp=str(dst_musicxml))

    return {
        "bpm": bpm,
        "duration_s_estimated": duration_s,
        "expected_notes_count": notes_count,
        "time_signature": ts,
        "key": key_name,
        "measures_kept": min(measures, total_measures) if total_measures else 0,
        "measures_total": total_measures,
    }


def render_wav_via_musescore(midi_path: Path, wav_path: Path) -> float:
    """Spustí MuseScore CLI: MIDI → WAV (44.1 kHz stereo float). Vrátí trvání v sekundách."""
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    # MuseScore odmítne přepsat – maž ručně
    if wav_path.exists():
        wav_path.unlink()
    cmd = [MUSESCORE, "-f", "-o", str(wav_path), str(midi_path)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=180,
    )
    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError(
            f"MuseScore selhal pro {midi_path.name}: rc={proc.returncode}\n"
            f"stderr (last 500 chars): {proc.stderr[-500:] if proc.stderr else ''}"
        )
    # Načti, převeď na mono 16-bit PCM, přepiš
    data, sr = sf.read(str(wav_path))
    if data.ndim == 2:
        mono = data.mean(axis=1)
    else:
        mono = data
    # Normalizace na rozumnou amplitudu (peak ~0.9)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 0:
        mono = mono * (0.9 / peak)
    sf.write(str(wav_path), mono.astype(np.float32), sr, subtype="PCM_16")
    return len(mono) / sr if sr else 0.0


# ---------- Per-piece pipeline ----------

@dataclass
class IndexEntry:
    slug: str
    category: str
    title: str
    composer: str
    license: str
    source_url: str
    mid_url: str
    bpm: float
    time_signature: str
    key: str
    expected_notes_count: int
    duration_s: float
    measures_kept: int
    measures_total: int
    musicxml: str  # relative path
    wav: str  # relative path
    json_meta: str  # relative path


def process_candidate(c: Candidate, http_sleep: float = 1.0) -> IndexEntry | None:
    """Stáhne, zkrátí a vyrenderuje 1 piece. Vrací IndexEntry, nebo None při skipu."""
    dst_dir = ROOT / c.category / c.slug
    dst_dir.mkdir(parents=True, exist_ok=True)
    midi_path = dst_dir / f"{c.slug}.midi"
    xml_path = dst_dir / f"{c.slug}.musicxml"
    wav_path = dst_dir / f"{c.slug}.wav"
    json_path = dst_dir / f"{c.slug}.json"

    # Pokud už existuje JSON, načti hotový záznam (idempotence).
    if json_path.exists() and xml_path.exists() and wav_path.exists():
        try:
            with json_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            entry = IndexEntry(**{k: data[k] for k in IndexEntry.__dataclass_fields__})
            logger.info("[skip] %s/%s už hotový", c.category, c.slug)
            return entry
        except Exception:
            logger.warning("Nevalidní JSON pro %s, regeneruji", c.slug)

    # Fetch meta
    try:
        meta = fetch_piece_meta(c.mutopia_id)
    except Exception as exc:
        logger.error("[fail] %s: piece-info chyba: %s", c.slug, exc)
        return None
    time.sleep(http_sleep)

    if not license_is_acceptable(meta.license):
        logger.warning(
            "[skip-license] %s (id=%d): %s – nevyhovuje PD/CC0/CC-BY",
            c.slug, c.mutopia_id, meta.license,
        )
        return None

    # Download MIDI
    src_midi = dst_dir / f"{c.slug}_source.midi"
    try:
        data = _http_get(meta.mid_url)
        src_midi.write_bytes(data)
    except Exception as exc:
        logger.error("[fail] %s: MIDI download: %s", c.slug, exc)
        return None
    time.sleep(http_sleep)

    # Truncate + MusicXML
    try:
        trunc_info = truncate_midi(src_midi, midi_path, xml_path, c.measures)
    except Exception as exc:
        logger.error("[fail] %s: truncate: %s", c.slug, exc)
        return None

    # Render WAV
    try:
        dur = render_wav_via_musescore(midi_path, wav_path)
    except Exception as exc:
        logger.error("[fail] %s: WAV render: %s", c.slug, exc)
        return None

    # Pokud render trvá >35 s, zkrátíme měřítka a re-renderujeme (max 2 retry).
    retry = 0
    while dur > 32.0 and retry < 3 and trunc_info.get("measures_kept", 0) > 2:
        retry += 1
        new_measures = max(2, int(trunc_info["measures_kept"] * 24.0 / dur))
        logger.info(
            "  -> %s je %.1fs (>32s), zkracuji na %d taktů (retry %d)",
            c.slug, dur, new_measures, retry,
        )
        try:
            trunc_info = truncate_midi(src_midi, midi_path, xml_path, new_measures)
            dur = render_wav_via_musescore(midi_path, wav_path)
        except Exception as exc:
            logger.error("[fail] %s: retry truncate: %s", c.slug, exc)
            return None

    # Uklidíme source midi (zmenší disk usage)
    try:
        src_midi.unlink()
    except OSError:
        pass

    entry = IndexEntry(
        slug=c.slug,
        category=c.category,
        title=meta.title or c.slug,
        composer=meta.composer or "Unknown",
        license=meta.license,
        source_url=meta.source_url,
        mid_url=meta.mid_url,
        bpm=trunc_info["bpm"],
        time_signature=trunc_info["time_signature"],
        key=trunc_info["key"],
        expected_notes_count=trunc_info["expected_notes_count"],
        duration_s=dur,
        measures_kept=trunc_info["measures_kept"],
        measures_total=trunc_info["measures_total"],
        musicxml=str(xml_path.relative_to(ROOT)),
        wav=str(wav_path.relative_to(ROOT)),
        json_meta=str(json_path.relative_to(ROOT)),
    )
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(entry), f, ensure_ascii=False, indent=2)
    logger.info(
        "[ok] %s/%s  %.1fs  notes=%d  bpm=%.1f  lic=%s",
        c.category, c.slug, dur, entry.expected_notes_count, entry.bpm, entry.license[:30],
    )
    return entry


def write_index(entries: list[IndexEntry]) -> Path:
    idx_path = ROOT / "INDEX.json"
    out = {
        "version": 1,
        "count": len(entries),
        "categories": sorted({e.category for e in entries}),
        "pieces": [asdict(e) for e in entries],
    }
    with idx_path.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return idx_path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not Path(MUSESCORE).exists():
        logger.error("MuseScore CLI nenalezen na %s", MUSESCORE)
        return 2

    only = set(os.environ.get("MUTOPIA_ONLY", "").split(",")) - {""}
    candidates = [c for c in CANDIDATES if not only or c.slug in only]
    logger.info("Zpracovávám %d kandidátů (z %d celkem)", len(candidates), len(CANDIDATES))

    entries: list[IndexEntry] = []
    failures: list[str] = []
    for c in candidates:
        try:
            e = process_candidate(c)
        except Exception as exc:
            logger.exception("Neošetřená chyba u %s: %s", c.slug, exc)
            e = None
        if e is None:
            failures.append(c.slug)
        else:
            entries.append(e)

    idx_path = write_index(entries)

    # Sumarizace
    total_bytes = 0
    for e in entries:
        for p in (ROOT / e.wav, ROOT / e.musicxml, ROOT / e.json_meta):
            if p.exists():
                total_bytes += p.stat().st_size
    logger.info("--- HOTOVO ---")
    logger.info("Úspěšně: %d / %d", len(entries), len(candidates))
    logger.info("Neúspěšně: %d (%s)", len(failures), ", ".join(failures) if failures else "-")
    logger.info("INDEX: %s", idx_path)
    logger.info("Celková velikost dat: %.1f MB", total_bytes / 1024 / 1024)
    return 0 if len(entries) >= 30 else 1


if __name__ == "__main__":
    raise SystemExit(main())
