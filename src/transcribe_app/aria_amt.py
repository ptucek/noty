"""Aria-AMT backend (piano-only SOTA AMT, EleutherAI).

Exponuje :func:`aria_amt_events` se stejným kontraktem jako ``_basic_pitch_events``:
``Path → list[NoteEvent]``. Volá se přes env var ``TRANSCRIPTION_BACKEND=aria_amt``
v :mod:`transcribe_app.transcribe`.

Aktuální stav: **integrace blokována** (viz ``docs/aria_amt_notes.md``).

Souhrn blokerů (květen 2026):

1. ``aria-amt`` (https://github.com/EleutherAI/aria-amt) je výzkumný repozitář bez PyPI
   wheelu. ``pip install`` z gitu vynutí ``torch<=2.5`` + ``torchaudio<=2.5``
   (testováno ``uv pip install --dry-run`` v tomto projektu → downgrade z 2.12 na
   2.5). To rozbije ostatní deps (basic-pitch, demucs) → nelze nainstalovat
   bez vlastního fork-u nebo `--no-deps` + ručního dořešení.

2. I po instalaci je inferenční pipeline (``amt/inference/transcribe.py``) hardcoded
   na CUDA: ``model.cuda()``, ``audio_seg.cuda()``, ``torch.autocast("cuda", ...)``,
   ``torch.cuda.is_bf16_supported``, ``torch.arange(..., device="cuda")`` v statickém
   masku, multiprocess GPU manager. Žádná CPU větev.

3. Modelové checkpointy (``loubb/aria-midi/piano-medium-double-1.0.safetensors``,
   ~1.5 GB) jsou bf16 → na CPU bez bf16 podpory by se musely převést na fp32.

Implementace tady proto: vyhodí :class:`RuntimeError` s jasným důvodem, takže
dispatcher v :mod:`transcribe_app.transcribe` zaloguje warning a spadne zpět na
Basic Pitch. Až bude k dispozici CPU-friendly port (např. ONNX export nebo
upstream PR), stačí přepsat tělo :func:`aria_amt_events`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .notation import NoteEvent

logger = logging.getLogger(__name__)


_BLOCKER_MSG = (
    "Aria-AMT integrace zatím blokována: "
    "(1) upstream pin torch<=2.5 koliduje s torch 2.12 v tomto projektu, "
    "(2) inference pipeline je hardcoded na CUDA bez CPU fallback. "
    "Detaily v docs/aria_amt_notes.md. "
    "Použij TRANSCRIPTION_BACKEND=basic_pitch (default) nebo doplň CPU port."
)


def aria_amt_events(audio_path: Path) -> list[NoteEvent]:
    """Spustí Aria-AMT na audio, vrátí list NoteEvent.

    Lazy-import heavy deps. Aktuálně vyhazuje :class:`RuntimeError` (viz docstring
    modulu) – dispatcher si toho všimne a spadne na Basic Pitch.
    """
    # Zkus zjistit, jestli je balík vůbec k dispozici (kdyby si někdo udělal fork
    # s CPU podporou a přidal `aria-amt` jako extra dep). Pokud ano, zkus volat
    # platform-agnostic helper z `amt.inference.transcribe`. Pokud neexistuje,
    # zlog warning + spadni do RuntimeError (dispatcher to chytne).
    try:
        import amt.inference.transcribe as _amt_inf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(_BLOCKER_MSG) from exc

    cpu_fn = getattr(_amt_inf, "transcribe_cpu", None)
    if cpu_fn is None:
        raise RuntimeError(_BLOCKER_MSG)

    logger.info("Aria-AMT transkripce (CPU port): %s", audio_path.name)
    note_events = cpu_fn(str(audio_path))  # type: ignore[misc]
    # Očekávaný formát z hypotetického CPU portu: list[tuple[start_s, end_s, midi, velocity]].
    return [
        NoteEvent(
            pitch_midi=int(midi),
            start_s=float(start),
            end_s=float(end),
            velocity=max(1, min(127, int(vel))),
        )
        for (start, end, midi, vel) in note_events
    ]
