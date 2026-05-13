"""Audio → notový zápis pipeline."""

import collections
import collections.abc
import logging
import warnings

import numpy as _np

# Madmom 0.16.1 (2022) importuje `from collections import MutableSequence` apod.,
# které byly v Py 3.10 přesunuty do collections.abc. Patchujeme zpět.
for _abc_name in (
    "MutableSequence",
    "MutableMapping",
    "Mapping",
    "Sequence",
    "Iterable",
    "Container",
    "Callable",
):
    if not hasattr(collections, _abc_name):
        setattr(collections, _abc_name, getattr(collections.abc, _abc_name))

# Madmom 0.16.1 (2022) používá deprecated `np.float`, `np.int` atd. (odstraněno v numpy 1.24+).
for _np_name, _np_target in (
    ("float", float),
    ("int", int),
    ("bool", bool),
    ("complex", complex),
    ("object", object),
    ("long", int),
):
    if not hasattr(_np, _np_name):
        setattr(_np, _np_name, _np_target)


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
