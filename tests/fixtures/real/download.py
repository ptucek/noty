"""Stáhne kurátorovaný dataset reálných klavírních kusů s engraved-source ground truth.

Zdroj: **Open Goldberg Variations** (Werner Schweer / Kimiko Ishizaka, CC0).
- **Skóre**: `goldberg.mscz` z `github.com/musescore/MuseScore/demos/` – originální
  engraved MuseScore source, který Werner Schweer připravil pro Open Goldberg
  projekt. **Loseless** konverze do MusicXML přes MuseScore CLI (na rozdíl
  od MIDI→MusicXML, které je kvantizací ztrátové).
- **Audio**: nahrávka Kimiko Ishizaka z `archive.org/details/OpenGoldbergVariations`
  (FLAC, CC0). Reálné piano (Bösendorfer 290 Imperial), studiová produkce.
- **Licence**: oba zdroje jsou **Creative Commons Zero (CC0, Public Domain)**
  – uvolněno 28. 5. 2012 projektem opengoldbergvariations.org.

## Pipeline pro každý kus

1. Stáhne `goldberg.mscz` (jedenkrát, ~150 KB).
2. Konvertuje na MusicXML přes MuseScore CLI (**lossless**).
3. Detekuje hranice 28 vět (Aria + 27 variací) podle resetu měřítka na 1.
4. Vyřízne prvních N taktů (default 8) → samostatný MusicXML soubor.
5. Stáhne Ishizaka FLAC z archive.org, ffmpeg ořeže na prvních ~30 s
   (mono, 22050 Hz, 16-bit PCM) → `<slug>.wav` – **toto je hlavní audio
   pro test pipeline**, reálná studiová nahrávka.
6. Volitelně (`OPENGOLDBERG_INCLUDE_SYNTH=1`) renderuje pomocný WAV přes
   MuseScore CLI z výřezu jako `<slug>_synth.wav` (pro A/B srovnání s
   reálnou nahrávkou).

**Ground truth**: MusicXML pro každý kus pochází přímo z MuseScore
engraved zdroje (.mscz). To je kanonická notační reprezentace – tempo,
takt, klíč, repetice a artikulace jsou autoritativně zadány autorem
edice. MIDI→MusicXML konverze music21 zde **není** použita.

**Caveat alignment**: Ishizaka přidává performance rubato (cca ±5-15 %
od skóre tempo). MusicXML ground truth má score-tempo, takže absolutní
pozice not v sekundách neodpovídá přesně, ale **posloupnost a pitch-
class** ano (což je to, co naše correctness testy porovnávají).

Spuštění:
    cd <projekt> && uv run python tests/fixtures/real/download.py

Volitelné env vars:
    REAL_ONLY=aria,var01,var05         # jen vybrané slugy
    OPENGOLDBERG_INCLUDE_SYNTH=1       # přidej i mscore-rendered _synth.wav
    OPENGOLDBERG_REAL_AUDIO=0          # přeskoč real audio (jen mscore render)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# music21 + soundfile/numpy přes projektové dependencies
from music21 import converter, stream  # noqa: E402
import soundfile as sf  # noqa: E402
import numpy as np  # noqa: E402

logger = logging.getLogger("real.download")

# --- Externí nástroje ---
MUSESCORE = "/Applications/MuseScore 4.app/Contents/MacOS/mscore"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

# --- Open Goldberg zdrojové URL ---
MSCZ_URL = "https://raw.githubusercontent.com/musescore/MuseScore/master/demos/goldberg.mscz"
ARCHIVE_PREFIX = (
    "https://archive.org/download/OpenGoldbergVariations/"
    "Kimiko%20Ishizaka%20-%20J.S.%20Bach-%20-Open-%20"
    "Goldberg%20Variations%2C%20BWV%20988%20%28Piano%29%20-%20"
)

USER_AGENT = "noty-fixtures/1.0 (Open Goldberg engraved-source fixtures)"

# --- Konfigurace ---
DEFAULT_MEASURES = 8  # počet taktů od počátku každé věty, které ground-truth zachová
REAL_AUDIO_SECONDS = 25.0  # délka Ishizaka výřezu

LICENSE_TEXT = "Creative Commons Zero (CC0, Public Domain)"
LICENSE_URL = "https://creativecommons.org/publicdomain/zero/1.0/"
SCORE_SOURCE = "Open Goldberg Variations (Werner Schweer, MuseScore CC0)"
AUDIO_SOURCE = "Kimiko Ishizaka recording (Bösendorfer 290 Imperial, Berlin 2012, CC0)"
GROUND_TRUTH_NOTE = (
    "MusicXML ground truth pochází z engraved MuseScore zdroje (.mscz → MusicXML "
    "přes MuseScore CLI, lossless). NENÍ použita MIDI→MusicXML konverze."
)


# ---------- Kandidáti ----------

@dataclass(frozen=True)
class Candidate:
    """Položka v Goldberg Variations.

    `movement_index` odkazuje na pozici ve výstupu z detekce hranic
    (0 = Aria, 1 = Variation 1, ...). `archive_track` je číslo stopy
    v archive.org listingu (1-31).
    """
    slug: str
    title: str
    movement_index: int  # 0 = Aria, 1..27 = další movements
    archive_track: int   # 1..31 pro Ishizaka FLACy
    archive_filename: str  # přesný název souboru v archive.org
    measures: int = DEFAULT_MEASURES


# Open Goldberg má 28 detekovaných movements v goldberg.mscz (Aria + 27 variací).
# Ishizaka nahrávka má 31 stop (Aria + 30 variací + Aria da Capo). Mapování je
# index_v_mscz → archive.org_track:  Aria=track1, Var_i=track(i+1), atd.
# Vybíráme 8 movements s různými taktovými značkami pro pokrytí test_time_signature.
#
# Mapování taktovek dle goldberg.mscz: 0=3/4 (Aria), 1=3/4 (Var1), 2=2/4 (Var2),
#   3=12/8 (Var3 Canone), 4=3/8 (Var4), 5=3/4 (Var5), 6=3/8 (Var6 Canone),
#   7=6/8 (Var7), 8=3/4 (Var8), 9=4/4 (Var9 Canone), 10=3/4 (Var10 Fughetta), ...
CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        "aria", "Aria",
        movement_index=0, archive_track=1,
        archive_filename="01 Aria.flac",
        measures=8,
    ),
    Candidate(
        "var01", "Variatio 1 a 1 Clav.",
        movement_index=1, archive_track=2,
        archive_filename="02 Variatio 1 a 1 Clav..flac",
        measures=8,
    ),
    Candidate(
        "var02", "Variatio 2 a 1 Clav.",
        movement_index=2, archive_track=3,
        archive_filename="03 Variatio 2 a 1 Clav..flac",
        measures=8,
    ),
    Candidate(
        "var03_canone", "Variatio 3 a 1 Clav. Canone all'Unisuono",
        movement_index=3, archive_track=4,
        archive_filename="04 Variatio 3 a 1 Clav. Canone all Unisuono.flac",
        measures=4,  # 12/8, kratší kus
    ),
    Candidate(
        "var04", "Variatio 4 a 1 Clav.",
        movement_index=4, archive_track=5,
        archive_filename="05 Variatio 4 a 1 Clav..flac",
        measures=8,
    ),
    Candidate(
        "var05", "Variatio 5 a 1 ovvero 2 Clav.",
        movement_index=5, archive_track=6,
        archive_filename="06 Variatio 5 a 1 ovvero 2 Clav..flac",
        measures=8,
    ),
    Candidate(
        "var07", "Variatio 7 a 1 ovvero 2 Clav. (al tempo di Giga)",
        movement_index=7, archive_track=8,
        archive_filename="08 Variatio 7 a 1 ovvero 2 Clav..flac",
        measures=8,
    ),
    Candidate(
        "var09_canone", "Variatio 9 a 1 Clav. Canone alla Terza",
        movement_index=9, archive_track=10,
        archive_filename="10 Variatio 9 a 1 Clav. Canone alla Terza.flac",
        measures=8,
    ),
    Candidate(
        "var10_fughetta", "Variatio 10 a 1 Clav. Fughetta",
        movement_index=10, archive_track=11,
        archive_filename="11 Variatio 10 a 1 Clav. Fughetta.flac",
        measures=8,
    ),
)


# ---------- HTTP helpers ----------

def _http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_download(url: str, dst: Path, timeout: float = 300.0) -> int:
    """Stream-download URL do `dst`. Vrací počet bajtů."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as r, dst.open("wb") as out:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    return total


# ---------- Skóre: mscz → MusicXML ----------

def ensure_full_goldberg_xml() -> Path:
    """Stáhne goldberg.mscz (CC0) a vrátí cestu k MusicXML konverzi.

    Idempotentní – už-existující soubory přeskočí.
    """
    mscz_path = ROOT / "_source" / "goldberg.mscz"
    full_xml = ROOT / "_source" / "goldberg.musicxml"
    if full_xml.exists() and full_xml.stat().st_size > 100_000:
        return full_xml

    mscz_path.parent.mkdir(parents=True, exist_ok=True)
    if not mscz_path.exists() or mscz_path.stat().st_size < 50_000:
        logger.info("Stahuji goldberg.mscz (CC0) z %s ...", MSCZ_URL)
        _http_download(MSCZ_URL, mscz_path)
        logger.info("Velikost goldberg.mscz: %d B", mscz_path.stat().st_size)

    logger.info("Konvertuji goldberg.mscz → MusicXML přes MuseScore CLI ...")
    cmd = [MUSESCORE, "-f", "-o", str(full_xml), str(mscz_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if not full_xml.exists() or full_xml.stat().st_size < 100_000:
        raise RuntimeError(
            f"MuseScore selhal: rc={proc.returncode}\n"
            f"stderr (last 500): {proc.stderr[-500:] if proc.stderr else ''}"
        )
    logger.info("MusicXML: %d B", full_xml.stat().st_size)
    return full_xml


def detect_movements(full_xml: Path) -> list[tuple[int, int, str]]:
    """Detekuje hranice movements v Goldberg score.

    Vrací list (start_index, end_index, time_signature) podle indexů v parts[0].
    Hranice = místo, kde se `Measure.measureNumber` resetuje na 1
    (vyjma první měření).
    """
    sc = converter.parse(str(full_xml))
    p0 = sc.parts[0]
    measures = list(p0.getElementsByClass("Measure"))

    movements: list[tuple[int, int, str]] = []
    cur_start = 0
    prev_num = -1
    for i, m in enumerate(measures):
        if m.measureNumber == 1 and i > 0 and prev_num != 0:
            # boundary
            ts_list = list(measures[cur_start].getElementsByClass("TimeSignature"))
            ts = f"{ts_list[0].numerator}/{ts_list[0].denominator}" if ts_list else "?"
            movements.append((cur_start, i - 1, ts))
            cur_start = i
        prev_num = m.measureNumber
    # poslední
    ts_list = list(measures[cur_start].getElementsByClass("TimeSignature"))
    ts = f"{ts_list[0].numerator}/{ts_list[0].denominator}" if ts_list else "?"
    movements.append((cur_start, len(measures) - 1, ts))
    return movements


def extract_movement_excerpt(
    full_xml: Path,
    movement_idx: int,
    measures_count: int,
    dst_xml: Path,
) -> dict[str, Any]:
    """Vyřízne první `measures_count` taktů z `movement_idx`-té věty a uloží MusicXML.

    Vrací metadata (bpm, time_signature, key, expected_notes_count, measures_kept,
    measures_total). Tempo: pokud věta nemá explicit MetronomeMark, použijeme
    fallback z named-tempo (Andante = 76, Allegro = 120, atd.) nebo 90.
    """
    sc = converter.parse(str(full_xml))
    movements = detect_movements(full_xml)
    if movement_idx >= len(movements):
        raise IndexError(
            f"movement_idx={movement_idx} mimo rozsah (max {len(movements) - 1})"
        )
    start_idx, end_idx, ts_str = movements[movement_idx]

    # Postavíme nový Score se stejným počtem partů, ale jen řez taktů.
    new_sc = stream.Score()
    new_sc.metadata = sc.metadata
    keep_count = min(measures_count, end_idx - start_idx + 1)

    # Pro každý part vyrobíme nový Part jen s vybranými takty.
    for part in sc.parts:
        all_meas = list(part.getElementsByClass("Measure"))
        if len(all_meas) <= end_idx:
            # Některé části mohou mít méně taktů – přeskoč
            continue
        new_part = stream.Part()
        new_part.id = part.id
        new_part.partName = part.partName
        # Klíč na začátku
        for el in part.flatten().getElementsByClass(("Clef", "Instrument", "KeySignature")):
            try:
                new_part.insert(0, el)
            except Exception:
                pass
        # Vlož vybrané takty (renumberujeme od 1)
        for j in range(keep_count):
            m = all_meas[start_idx + j]
            mcopy = m  # mělká kopie reference – music21 si pak vyřeší offsets
            mcopy.number = j + 1
            new_part.append(mcopy)
        new_sc.insert(0, new_part)

    # Tempo detekce
    bpm = 90.0
    mm_list = list(new_sc.recurse().getElementsByClass("MetronomeMark"))
    if not mm_list:
        # zkus z původního score na začátku tohoto movementu
        for el in sc.recurse().getElementsByClass("MetronomeMark"):
            bpm = float(el.getQuarterBPM() or el.number or bpm)
            break
    else:
        try:
            bpm = float(mm_list[0].getQuarterBPM())
        except Exception:
            try:
                bpm = float(mm_list[0].number)  # type: ignore[union-attr]
            except Exception:
                pass

    # Time signature
    if not list(new_sc.recurse().getElementsByClass("TimeSignature")):
        # Vlož ručně, aby renderování nepoužilo default 4/4
        try:
            from music21 import meter
            num, den = ts_str.split("/")
            ts_obj = meter.TimeSignature(f"{num}/{den}")
            new_sc.parts[0].insert(0, ts_obj)
        except Exception:
            pass

    # Key
    key_name = "?"
    try:
        k = new_sc.analyze("key")
        key_name = f"{k.tonic.name} {k.mode}"
    except Exception:
        pass

    # Notes count
    notes_count = sum(1 for _ in new_sc.recurse().notes)

    dst_xml.parent.mkdir(parents=True, exist_ok=True)
    new_sc.write("musicxml", fp=str(dst_xml))

    # Estimated duration
    ql = float(new_sc.duration.quarterLength)
    duration_s = ql * 60.0 / bpm if bpm > 0 else 0.0

    return {
        "bpm": bpm,
        "time_signature": ts_str,
        "key": key_name,
        "expected_notes_count": notes_count,
        "measures_kept": keep_count,
        "measures_total": end_idx - start_idx + 1,
        "duration_s_estimated": duration_s,
    }


def render_wav_via_musescore(src_xml: Path, dst_wav: Path) -> float:
    """MuseScore CLI: MusicXML → WAV (44.1 kHz stereo float). Vrací trvání v sekundách.

    Po renderu konvertuje na mono 16-bit PCM pro menší velikost.
    """
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    if dst_wav.exists():
        dst_wav.unlink()
    cmd = [MUSESCORE, "-f", "-o", str(dst_wav), str(src_xml)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not dst_wav.exists() or dst_wav.stat().st_size == 0:
        raise RuntimeError(
            f"MuseScore selhal pro {src_xml.name}: rc={proc.returncode}\n"
            f"stderr: {proc.stderr[-500:] if proc.stderr else ''}"
        )
    # Stereo → mono, 16-bit PCM, normalizace na peak ~0.9
    data, sr = sf.read(str(dst_wav))
    mono = data.mean(axis=1) if data.ndim == 2 else data
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 0:
        mono = mono * (0.9 / peak)
    sf.write(str(dst_wav), mono.astype(np.float32), sr, subtype="PCM_16")
    return len(mono) / sr if sr else 0.0


def fetch_ishizaka_real_wav(c: Candidate, dst_wav: Path, seconds: float) -> float:
    """Stáhne Ishizaka FLAC z archive.org a vyřízne prvních `seconds` s.

    Výstup: mono 22050 Hz 16-bit PCM (menší velikost než 44.1 kHz stereo
    24-bit). Vrací skutečné trvání.

    Pozn.: stahuje plný FLAC track (typicky 20-80 MB), pak ho ořeže.
    Pro úsporu místa by šlo použít HTTP Range na FLAC chunky, ale to
    je netriviální (FLAC stream parsing). FLAC zdroj nepřežije v cache.
    """
    from urllib.parse import quote
    url = ARCHIVE_PREFIX + quote(c.archive_filename, safe="")
    tmp_flac = dst_wav.with_suffix(".tmp.flac")
    try:
        logger.info("[%s] Stahuji Ishizaka FLAC (~%.0f MB) ...", c.slug, 0)
        _http_download(url, tmp_flac)
        logger.info("[%s] FLAC stažen (%d B)", c.slug, tmp_flac.stat().st_size)

        # ffmpeg: prvních `seconds` s, mono, 22050 Hz, 16-bit PCM
        if dst_wav.exists():
            dst_wav.unlink()
        cmd = [
            FFMPEG, "-y", "-loglevel", "error",
            "-ss", "0", "-t", f"{seconds:.3f}",
            "-i", str(tmp_flac),
            "-ac", "1",
            "-ar", "22050",
            "-sample_fmt", "s16",
            str(dst_wav),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if not dst_wav.exists():
            raise RuntimeError(f"ffmpeg selhal: rc={proc.returncode} stderr={proc.stderr[-300:]}")
        info = sf.info(str(dst_wav))
        return float(info.frames) / float(info.samplerate)
    finally:
        if tmp_flac.exists():
            try:
                tmp_flac.unlink()
            except OSError:
                pass


# ---------- Per-piece pipeline ----------

@dataclass
class IndexEntry:
    slug: str
    category: str
    title: str
    composer: str
    performer: str
    license: str
    license_url: str
    source: str
    score_source: str
    audio_source: str
    ground_truth_note: str
    bpm: float
    time_signature: str
    key: str
    expected_notes_count: int
    duration_s: float
    measures_kept: int
    measures_total: int
    musicxml: str
    wav: str  # hlavní audio = reálná Ishizaka nahrávka, nebo (fallback) mscore-render
    audio_type: str  # "real" nebo "synth"
    wav_synth: str | None  # volitelný mscore-rendered backup pro A/B srovnání
    json_meta: str


def process_candidate(
    c: Candidate,
    full_xml: Path,
    use_real_audio: bool,
    include_synth: bool,
) -> IndexEntry | None:
    """Vyřízne MusicXML pro 1 movement a produkuje WAV.

    Pokud `use_real_audio`, hlavní `<slug>.wav` je výřez Ishizaka FLAC.
    Jinak je hlavní `<slug>.wav` mscore-rendered z výřezu MusicXML.
    Pokud `include_synth`, vyrobí navíc `<slug>_synth.wav` (mscore render)
    pro A/B srovnání i v real-audio režimu.
    """
    dst_dir = ROOT / c.slug
    dst_dir.mkdir(parents=True, exist_ok=True)
    xml_path = dst_dir / f"{c.slug}.musicxml"
    wav_path = dst_dir / f"{c.slug}.wav"
    wav_synth_path = dst_dir / f"{c.slug}_synth.wav"
    json_path = dst_dir / f"{c.slug}.json"

    # Idempotence: pokud máme XML+WAV+JSON, načti.
    if json_path.exists() and xml_path.exists() and wav_path.exists():
        try:
            with json_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            entry = IndexEntry(**{
                k: data.get(k) for k in IndexEntry.__dataclass_fields__
            })
            # Pokud chce uživatel synth audio a my ho ještě nemáme, dotvor
            if include_synth and (entry.wav_synth is None or not wav_synth_path.exists()):
                try:
                    render_wav_via_musescore(xml_path, wav_synth_path)
                    entry.wav_synth = f"{c.slug}/{c.slug}_synth.wav"
                    with json_path.open("w", encoding="utf-8") as f:
                        json.dump(asdict(entry), f, ensure_ascii=False, indent=2)
                except Exception as exc:
                    logger.warning("[%s] synth audio render failed: %s", c.slug, exc)
            logger.info("[skip] %s už hotový (%s audio)", c.slug, entry.audio_type)
            return entry
        except Exception as exc:
            logger.warning("Nevalidní JSON pro %s (%s), regeneruji", c.slug, exc)

    # 1. Extrahuj movement excerpt → MusicXML (engraved ground truth)
    try:
        meta = extract_movement_excerpt(full_xml, c.movement_index, c.measures, xml_path)
    except Exception as exc:
        logger.error("[fail] %s: extract movement: %s", c.slug, exc)
        return None

    audio_type = "real" if use_real_audio else "synth"
    wav_dur: float
    wav_synth_rel: str | None = None

    if use_real_audio:
        # 2a. Stáhni Ishizaka FLAC + ořež → hlavní WAV
        try:
            wav_dur = fetch_ishizaka_real_wav(c, wav_path, REAL_AUDIO_SECONDS)
            logger.info("[%s] Ishizaka real audio: %.2fs", c.slug, wav_dur)
        except Exception as exc:
            logger.warning(
                "[%s] real audio fetch fail: %s, fallback na mscore render",
                c.slug, exc,
            )
            try:
                wav_dur = render_wav_via_musescore(xml_path, wav_path)
                audio_type = "synth"
            except Exception as exc2:
                logger.error("[fail] %s: i synth fallback selhal: %s", c.slug, exc2)
                return None

        # 2b. Optional: synth WAV pro A/B srovnání
        if include_synth:
            try:
                render_wav_via_musescore(xml_path, wav_synth_path)
                wav_synth_rel = f"{c.slug}/{c.slug}_synth.wav"
            except Exception as exc:
                logger.warning("[%s] synth render fail: %s", c.slug, exc)
    else:
        # Jen mscore render → hlavní WAV
        try:
            wav_dur = render_wav_via_musescore(xml_path, wav_path)
        except Exception as exc:
            logger.error("[fail] %s: WAV render: %s", c.slug, exc)
            return None

    entry = IndexEntry(
        slug=c.slug,
        category="klavir",
        title=c.title,
        composer="Johann Sebastian Bach (1685–1750)",
        performer="Kimiko Ishizaka (Bösendorfer 290 Imperial, Berlin 2012)",
        license=LICENSE_TEXT,
        license_url=LICENSE_URL,
        source="Open Goldberg Variations (opengoldbergvariations.org, 2012)",
        score_source=SCORE_SOURCE,
        audio_source=AUDIO_SOURCE,
        ground_truth_note=GROUND_TRUTH_NOTE,
        bpm=meta["bpm"],
        time_signature=meta["time_signature"],
        key=meta["key"],
        expected_notes_count=meta["expected_notes_count"],
        duration_s=wav_dur,
        measures_kept=meta["measures_kept"],
        measures_total=meta["measures_total"],
        musicxml=f"{c.slug}/{c.slug}.musicxml",
        wav=f"{c.slug}/{c.slug}.wav",
        audio_type=audio_type,
        wav_synth=wav_synth_rel,
        json_meta=f"{c.slug}/{c.slug}.json",
    )
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(entry), f, ensure_ascii=False, indent=2)
    logger.info(
        "[ok] %s  %.1fs  notes=%d  bpm=%.1f  ts=%s  key=%s  audio=%s",
        c.slug, wav_dur, entry.expected_notes_count, entry.bpm,
        entry.time_signature, entry.key, entry.audio_type,
    )
    return entry


def write_index(entries: list[IndexEntry]) -> Path:
    idx_path = ROOT / "INDEX.json"
    out = {
        "version": 2,
        "count": len(entries),
        "source": "Open Goldberg Variations (BWV 988)",
        "score_source_url": MSCZ_URL,
        "audio_source_url": "https://archive.org/details/OpenGoldbergVariations",
        "license": LICENSE_TEXT,
        "license_url": LICENSE_URL,
        "ground_truth_methodology": (
            "MusicXML pochází z engraved MuseScore zdroje (.mscz → MusicXML, "
            "lossless přes MuseScore CLI). NENÍ použita MIDI→MusicXML konverze. "
            "WAV pro test pipeline je rendrované z MusicXML přes MuseScore CLI "
            "(přesné timing-aligned ke skóre). `wav_real` (volitelné) je výřez "
            "Ishizaka studio nahrávky z archive.org."
        ),
        "categories": ["klavir"],
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

    use_real_audio = os.environ.get("OPENGOLDBERG_REAL_AUDIO", "1") == "1"
    include_synth = os.environ.get("OPENGOLDBERG_INCLUDE_SYNTH", "0") == "1"
    if use_real_audio and not Path(FFMPEG).exists():
        logger.error(
            "OPENGOLDBERG_REAL_AUDIO=1 vyžaduje ffmpeg na %s", FFMPEG,
        )
        return 2

    only_env = os.environ.get("REAL_ONLY", "").strip()
    only_set = set(only_env.split(",")) - {""} if only_env else set()
    cands = [c for c in CANDIDATES if not only_set or c.slug in only_set]
    logger.info("Zpracovávám %d kandidátů (z %d celkem)", len(cands), len(CANDIDATES))
    logger.info(
        "Hlavní audio: %s | Include synth: %s",
        "Ishizaka real FLAC" if use_real_audio else "MuseScore render",
        include_synth,
    )

    # Stáhni + konvertuj plný score jednou
    try:
        full_xml = ensure_full_goldberg_xml()
    except Exception as exc:
        logger.exception("Nelze získat plný Goldberg MusicXML: %s", exc)
        return 1

    # Diagnostické info o movements
    try:
        movements = detect_movements(full_xml)
        logger.info("Detekováno %d movements v goldberg.mscz", len(movements))
    except Exception:
        pass

    entries: list[IndexEntry] = []
    failures: list[str] = []
    for c in cands:
        try:
            e = process_candidate(c, full_xml, use_real_audio, include_synth)
        except Exception as exc:
            logger.exception("Neošetřená chyba u %s: %s", c.slug, exc)
            e = None
        if e is None:
            failures.append(c.slug)
        else:
            entries.append(e)

    idx_path = write_index(entries)

    total_bytes = 0
    for e in entries:
        for rel in (e.wav, e.musicxml, e.json_meta):
            p = ROOT / rel
            if p.exists():
                total_bytes += p.stat().st_size
        if e.wav_synth:
            p = ROOT / e.wav_synth
            if p.exists():
                total_bytes += p.stat().st_size
    logger.info("--- HOTOVO ---")
    logger.info("Úspěšně: %d / %d", len(entries), len(cands))
    if failures:
        logger.info("Neúspěšně: %s", ", ".join(failures))
    logger.info("INDEX: %s", idx_path)
    logger.info("Celková velikost dat: %.1f MB", total_bytes / 1024 / 1024)
    return 0 if entries else 1


if __name__ == "__main__":
    raise SystemExit(main())
