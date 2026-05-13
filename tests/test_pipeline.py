"""End-to-end smoke test celé pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import soundfile as sf

from transcribe_app.notation import events_to_musicxml
from transcribe_app.render import render_musicxml
from transcribe_app.transcribe import transcribe


def synth_c_major_scale(duration_per_note: float = 0.4, sr: int = 22050) -> tuple[np.ndarray, int]:
    """Vygeneruje monofonní C-dur stupnici (8 not) jako sinusoidy."""
    freqs = [261.63, 293.66, 329.63, 349.23, 392.00, 440.00, 493.88, 523.25]
    audio = np.concatenate(
        [_sine(f, duration_per_note, sr) for f in freqs]
    )
    return audio, sr


def _sine(freq: float, dur: float, sr: int) -> np.ndarray:
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    envelope = np.minimum(1.0, 10.0 * np.minimum(t, dur - t))
    return 0.5 * envelope * np.sin(2 * np.pi * freq * t)


def test_full_pipeline_klavir(tmp_path: Path) -> None:
    audio, sr = synth_c_major_scale()
    wav_path = tmp_path / "scale.wav"
    sf.write(str(wav_path), audio, sr)

    result = transcribe(wav_path, "klavir", tmp_path)
    assert len(result.events) > 0, "Basic Pitch nenašel žádné noty"
    print(f"  Detected {len(result.events)} notes, tempo {result.tempo_bpm:.1f} BPM")

    mxl_path = events_to_musicxml(result.events, tmp_path, bpm=result.tempo_bpm)
    assert mxl_path.exists() and mxl_path.stat().st_size > 0

    rendered = render_musicxml(mxl_path, tmp_path)
    assert "pdf" in rendered, f"Render výstupy: {list(rendered)}"
    assert "png" in rendered
    assert rendered["pdf"].stat().st_size > 1000


if __name__ == "__main__":
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="noty_smoke_"))
    print(f"Workdir: {tmp}")
    test_full_pipeline_klavir(tmp)
    print("✓ End-to-end pipeline OK")
    print(f"Soubory v: {tmp}")
    for p in sorted(tmp.iterdir()):
        print(f"  {p.name} ({p.stat().st_size} B)")
