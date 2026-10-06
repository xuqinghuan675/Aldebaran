from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.paths import HOME

CONFIG_FILE = HOME / 'intel_config.json'
SECURE_MODE_FIELD = 'credential_store_mode'
SECURE_MODE_DPAPI = 'dpapi'
DPAPI_PREFIX = 'dpapi:'
CREDENTIAL_FIELDS = (
    'deepseek_api_key',
    'github_token',
    'tickflow_token',
    'tickflow_free_token',
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoreResult:
    ok: bool
    error: str = ''
    code: str = ''


class CredentialStoreError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_windows() -> bool:
    return os.name == 'nt'


def _config_path(config_file: Path | None = None) -> Path:
    return Path(config_file) if config_file is not None else CONFIG_FILE


def _load_config(config_file: Path) -> dict[str, Any]:
    try:
        if config_file.exists():
            cfg = json.loads(config_file.read_text(encoding='utf-8'))
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        logger.warning('intel_config.json read failed; using empty config', exc_info=True)
    return {}


def _write_config(config_file: Path, cfg: dict[str, Any]) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _secure_mode_enabled(cfg: dict[str, Any]) -> bool:
    return cfg.get(SECURE_MODE_FIELD) == SECURE_MODE_DPAPI


def _require_dpapi_available() -> None:
    if not _is_windows():
        raise CredentialStoreError(
            'dpapi_unavailable',
            'Secure credential storage requires Windows DPAPI.',
        )
    if not hasattr(ctypes, 'WinDLL'):
        raise CredentialStoreError(
            'dpapi_unavailable',
            'Windows DPAPI is not available from this Python runtime.',
        )


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ('cbData', wintypes.DWORD),
        ('pbData', ctypes.POINTER(ctypes.c_char)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi_libraries() -> tuple[Any, Any]:
    _require_dpapi_available()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    blob_ptr = ctypes.POINTER(_DATA_BLOB)
    crypt32.CryptProtectData.argtypes = [
        blob_ptr,
        wintypes.LPCWSTR,
        blob_ptr,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        blob_ptr,
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_ptr,
        ctypes.POINTER(wintypes.LPWSTR),
        blob_ptr,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        blob_ptr,
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def _last_dpapi_error(action: str) -> CredentialStoreError:
    err = ctypes.get_last_error()
    return CredentialStoreError('dpapi_failed', f'{action} failed with Windows error {err}.')


def _protect_secret(secret: str) -> str:
    crypt32, kernel32 = _dpapi_libraries()
    in_blob, _buffer = _blob_from_bytes(secret.encode('utf-8'))
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        'Aldebaran credential',
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise _last_dpapi_error('CryptProtectData')
    try:
        payload = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return DPAPI_PREFIX + base64.b64encode(payload).decode('ascii')


def _unprotect_secret(payload: bytes) -> str:
    crypt32, kernel32 = _dpapi_libraries()
    in_blob, _buffer = _blob_from_bytes(payload)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise _last_dpapi_error('CryptUnprotectData')
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode('utf-8')
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _verify_dpapi_roundtrip() -> None:
    probe = 'aldebaran-dpapi-probe'
    protected = _protect_secret(probe)
    if not protected.startswith(DPAPI_PREFIX):
        raise CredentialStoreError(
            'dpapi_failed',
            'Windows DPAPI self-check did not return a DPAPI payload.',
        )
    try:
        payload = base64.b64decode(protected[len(DPAPI_PREFIX):], validate=True)
    except Exception as exc:
        raise CredentialStoreError(
            'dpapi_failed',
            'Windows DPAPI self-check returned invalid ciphertext.',
        ) from exc
    restored = _unprotect_secret(payload)
    if restored != probe:
        raise CredentialStoreError(
            'dpapi_failed',
            'Windows DPAPI self-check could not restore the probe payload.',
        )


def _decrypt_value(raw_value: Any, *, require_dpapi: bool, field: str) -> str:
    if not isinstance(raw_value, str):
        return ''
    value = raw_value.strip()
    if not value:
        return ''
    if value.startswith(DPAPI_PREFIX):
        encoded = value[len(DPAPI_PREFIX):].strip()
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise CredentialStoreError(
                'dpapi_invalid_ciphertext',
                f'{field} has invalid DPAPI ciphertext.',
            ) from exc
        return _unprotect_secret(payload).strip()
    if require_dpapi:
        raise CredentialStoreError(
            'plaintext_in_secure_mode',
            f'{field} is plaintext while secure mode is enabled.',
        )
    return value


def load_value(config_file: Path, field: str, env_var: str) -> str:
    env_value = os.environ.get(env_var, '').strip()
    if env_value:
        return env_value

    path = _config_path(config_file)
    cfg = _load_config(path)
    try:
        return _decrypt_value(
            cfg.get(field, ''),
            require_dpapi=_secure_mode_enabled(cfg),
            field=field,
        )
    except CredentialStoreError as exc:
        logger.warning('Credential read failed for %s: %s', field, exc.message, exc_info=True)
        return ''


def save_value(
    config_file: Path,
    field: str,
    value: str,
    *,
    log_errors: bool = True,
    raise_errors: bool = False,
) -> bool:
    path = _config_path(config_file)
    try:
        cfg = _load_config(path)
        cleaned = value.strip()
        if _secure_mode_enabled(cfg) and cleaned:
            cfg[field] = _protect_secret(cleaned)
        else:
            cfg[field] = cleaned
        _write_config(path, cfg)
        return True
    except CredentialStoreError as exc:
        if raise_errors:
            raise
        if log_errors:
            logger.warning('Credential save failed for %s: %s', field, exc.message, exc_info=True)
    except Exception:
        if raise_errors:
            raise
        if log_errors:
            logger.warning('Credential save failed for %s', field, exc_info=True)
    return False


def is_secure_mode(config_file: Path | None = None) -> bool:
    return _secure_mode_enabled(_load_config(_config_path(config_file)))


def enable_secure_mode(config_file: Path | None = None) -> StoreResult:
    path = _config_path(config_file)
    try:
        _require_dpapi_available()
        _verify_dpapi_roundtrip()
        cfg = _load_config(path)
        new_cfg = dict(cfg)
        for field in CREDENTIAL_FIELDS:
            value = _decrypt_value(
                new_cfg.get(field, ''),
                require_dpapi=False,
                field=field,
            )
            if value:
                new_cfg[field] = _protect_secret(value)
        new_cfg[SECURE_MODE_FIELD] = SECURE_MODE_DPAPI
        _write_config(path, new_cfg)
        return StoreResult(ok=True)
    except CredentialStoreError as exc:
        logger.warning('Secure credential mode was not enabled: %s', exc.message, exc_info=True)
        return StoreResult(ok=False, error=exc.message, code=exc.code)
    except Exception as exc:
        logger.warning('Secure credential mode was not enabled', exc_info=True)
        return StoreResult(ok=False, error=str(exc), code='config_write_failed')


def disable_secure_mode(config_file: Path | None = None) -> StoreResult:
    path = _config_path(config_file)
    try:
        cfg = _load_config(path)
        new_cfg = dict(cfg)
        for field in CREDENTIAL_FIELDS:
            value = _decrypt_value(
                new_cfg.get(field, ''),
                require_dpapi=False,
                field=field,
            )
            new_cfg[field] = value
        new_cfg.pop(SECURE_MODE_FIELD, None)
        _write_config(path, new_cfg)
        return StoreResult(ok=True)
    except CredentialStoreError as exc:
        logger.warning('Secure credential mode was not disabled: %s', exc.message, exc_info=True)
        return StoreResult(ok=False, error=exc.message, code=exc.code)
    except Exception as exc:
        logger.warning('Secure credential mode was not disabled', exc_info=True)
        return StoreResult(ok=False, error=str(exc), code='config_write_failed')
