"""Thin wrapper for the external Scrapling runtime.

The main Aldebaran process must not import Scrapling or browser dependencies.
This module invokes an isolated Python interpreter configured via
ALDEBARAN_SCRAPLING_PYTHON or the runtime_python argument.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


_DEFAULT_PROBE = Path(__file__).resolve().parents[2] / 'tools' / 'scrapling_fetch_probe.py'
_VALID_MODES = {'static', 'dynamic', 'stealth'}


def run_scrapling_fetch(
    url: str,
    *,
    mode: str = 'static',
    selectors: dict[str, str] | None = None,
    timeout_seconds: int = 25,
    runtime_python: str | os.PathLike[str] | None = None,
    probe_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Fetch one public page through the external Scrapling probe.

    Returns a JSON-like status dict in all cases. Failures are represented as
    ``ok: false`` so collector callers can isolate the source failure.
    """
    clean_url = str(url or '').strip()
    clean_mode = str(mode or 'static').strip().lower()
    if clean_mode not in _VALID_MODES:
        return _failure(clean_url, clean_mode or mode, f'invalid mode: {mode}')
    if not clean_url:
        return _failure(clean_url, clean_mode, 'url is required')

    runtime_value = runtime_python or os.environ.get('ALDEBARAN_SCRAPLING_PYTHON')
    if not runtime_value:
        return _failure(
            clean_url,
            clean_mode,
            'Scrapling runtime is not configured; set ALDEBARAN_SCRAPLING_PYTHON',
        )
    runtime = Path(runtime_value)
    probe = Path(probe_path or _DEFAULT_PROBE)
    if not runtime.exists():
        return _failure(clean_url, clean_mode, f'runtime not found: {runtime}')
    if not probe.exists():
        return _failure(clean_url, clean_mode, f'probe not found: {probe}')

    timeout = max(3, int(timeout_seconds or 25))
    command = [
        str(runtime),
        str(probe),
        '--url',
        clean_url,
        '--mode',
        clean_mode,
        '--timeout',
        str(timeout),
        '--json',
    ]
    if selectors:
        command.extend(['--selectors-json', json.dumps(selectors, ensure_ascii=False)])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return _failure(clean_url, clean_mode, f'timeout after {timeout_seconds}s')
    except OSError as exc:
        return _failure(clean_url, clean_mode, str(exc))

    stdout = (completed.stdout or '').strip()
    stderr = (completed.stderr or '').strip()
    if completed.returncode != 0:
        return _failure(clean_url, clean_mode, stderr or stdout or f'exit code {completed.returncode}')
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return _failure(clean_url, clean_mode, 'probe returned invalid JSON', stdout=stdout[:500], stderr=stderr[:500])
    if not isinstance(payload, dict):
        return _failure(clean_url, clean_mode, 'probe returned non-object JSON')
    payload.setdefault('ok', False)
    payload.setdefault('url', clean_url)
    payload.setdefault('mode', clean_mode)
    payload.setdefault('error', '')
    return payload


def _failure(url: str, mode: str, error: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'ok': False,
        'url': url,
        'mode': str(mode or 'static'),
        'title': '',
        'text': '',
        'items': [],
        'error': error,
    }
    payload.update(extra)
    return payload
