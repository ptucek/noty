"""Načtení audia, resample na cílový sample rate, mono."""

from __future__ import annotations

import logging
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

TARGET_SR = 22050
MAX_DURATION_S = 60.0


def load_audio(path: Path, sr: int = TARGET_SR, max_seconds: float = MAX_DURATION_S) -> tuple[np.ndarray, int]:
    """Načte audio (jakýkoliv formát co librosa zvládá), převede na mono @ sr, ořízne na max_seconds."""
    audio, src_sr = librosa.load(str(path), sr=sr, mono=True, duration=max_seconds)
    logger.info("Načteno audio: %s, %.1fs @ %d Hz (zdroj %d Hz)", path.name, len(audio) / sr, sr, src_sr)
    return audio, sr


def save_wav(audio: np.ndarray, sr: int, out_path: Path) -> Path:
    """Uloží numpy audio jako WAV. Vrací out_path."""
    sf.write(str(out_path), audio, sr, subtype="PCM_16")
    return out_path
