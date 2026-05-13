"""MusicXML → PDF/PNG/SVG/MID přes MuseScore 4 CLI."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_MAC_DEFAULT = Path("/Applications/MuseScore 4.app/Contents/MacOS/mscore")
_LINUX_DEFAULTS = (
    Path("/usr/bin/mscore"),
    Path("/usr/bin/musescore4"),
    Path("/usr/bin/musescore"),
)
_PAGED_FORMATS = {"png", "svg"}


def _find_musescore() -> Path:
    env_path = os.environ.get("MUSESCORE_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    if _MAC_DEFAULT.exists():
        return _MAC_DEFAULT
    for p in _LINUX_DEFAULTS:
        if p.exists():
            return p
    for name in ("mscore", "musescore4"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise FileNotFoundError(
        "MuseScore nenalezen. Nastav MUSESCORE_PATH nebo nainstaluj MuseScore 4."
    )


def render_musicxml(
    musicxml_path: Path,
    output_dir: Path,
    formats: tuple[str, ...] = ("pdf", "png", "svg", "mid"),
    basename: str = "transcription",
) -> dict[str, Path]:
    """Spustí MuseScore CLI, vyrenderuje výstupy do output_dir. Vrátí dict format → cesta."""
    mscore = _find_musescore()
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    for fmt in formats:
        target = output_dir / f"{basename}.{fmt}"
        try:
            subprocess.run(
                [str(mscore), "-o", str(target), str(musicxml_path)],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except subprocess.CalledProcessError as e:
            logger.warning(
                "MuseScore selhal pro formát %s: %s", fmt, e.stderr.decode(errors="replace")
            )
            continue
        except subprocess.TimeoutExpired:
            logger.warning("MuseScore timeout pro formát %s", fmt)
            continue
        if fmt in _PAGED_FORMATS:
            pages = sorted(output_dir.glob(f"{basename}-*.{fmt}"))
            if pages:
                results[fmt] = pages[0]
            elif target.exists():
                results[fmt] = target
            else:
                logger.warning("Žádný výstup pro formát %s", fmt)
        else:
            if target.exists():
                results[fmt] = target
            else:
                logger.warning("Výstupní soubor nenalezen pro formát %s", fmt)
    return results
