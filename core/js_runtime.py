"""Helpers for diagnosing optional JavaScript runtime availability."""
from __future__ import annotations

import os
import sys

_STUB_ENV = 'ALDEBARAN_PY_MINI_RACER_STUBBED'
FALLBACK_MESSAGE = 'JS runtime unavailable; using fallback parser'


def is_py_mini_racer_stubbed() -> bool:
    """Return True when app.py installed the py_mini_racer fallback stub."""
    module = sys.modules.get('py_mini_racer')
    if getattr(module, '__aldebaran_stubbed__', False):
        return True
    return os.environ.get(_STUB_ENV) == '1'


def js_runtime_diagnostic() -> str:
    """Return a short diagnostic string for logs, or empty when JS is available."""
    if is_py_mini_racer_stubbed():
        return FALLBACK_MESSAGE
    return ''
