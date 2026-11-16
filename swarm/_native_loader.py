"""Import gate for the required PyO3 extension ``swarm._native``.

The extension is required for every install. ``load_native`` never falls back to pure Python:
when the extension cannot be imported it raises an ``ImportError`` that names the install step.
``SWARM_PURE_PYTHON=1`` skips the native import; the test suite sets it so the pure-Python
oracles run without building the extension.
"""
from __future__ import annotations

import importlib
import os
from types import ModuleType

_MODULE = "swarm._native"
_MISSING_TEXT = f"No module named '{_MODULE}'"

PURE_PYTHON_ENV = "SWARM_PURE_PYTHON"

_INSTALL_HINT = (
    "swarm requires its native extension swarm._native (PyO3). Build and install it by running "
    "`pip install .` from the repository root with Python 3.12+ on Linux x86_64, then retry. "
    "Tests that deliberately run the pure-Python oracles may set {env}=1 instead."
).format(env=PURE_PYTHON_ENV)


def pure_python_requested() -> bool:
    """Return True when ``SWARM_PURE_PYTHON`` is exactly ``"1"``."""
    return os.environ.get(PURE_PYTHON_ENV) == "1"


def load_native() -> ModuleType:
    """Import and return ``swarm._native``; raise an actionable ImportError when it is unavailable."""
    try:
        return importlib.import_module(_MODULE)
    except ImportError as exc:
        if str(exc) == _MISSING_TEXT:
            raise ImportError(_INSTALL_HINT) from None
        raise ImportError(f"{_INSTALL_HINT} Import failed with: {exc}") from exc
