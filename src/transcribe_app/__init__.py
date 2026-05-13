"""Audio → notový zápis pipeline."""

import logging
import warnings


class _NoiseFilter(logging.Filter):
    """Vyfiltruje neškodné hlášky o nedostupných ML backendech — používáme ONNX."""

    _NOISE = (
        "tflite-runtime is not installed",
        "Tensorflow is not installed",
        "onnxruntime is not installed",
        "scikit-learn version",
        "Torch version",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(needle in msg for needle in self._NOISE)


logging.getLogger("coremltools").setLevel(logging.ERROR)
logging.getLogger().addFilter(_NoiseFilter())
warnings.filterwarnings("ignore", module="coremltools")
warnings.filterwarnings("ignore", message=".*pkg_resources.*")
