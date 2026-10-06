"""Credential access helpers for DeepSeek, GitHub, and TickFlow tokens.

The public load_* / save_* functions in this module are kept for existing
callers. Storage details live in core.credential_store.
"""
from __future__ import annotations

import logging

from core import credential_store
from core.paths import HOME

_CONFIG_FILE = HOME / 'intel_config.json'
_ENV_VAR = 'DEEPSEEK_API_KEY'
logger = logging.getLogger(__name__)


def load_api_key() -> str:
    """Load the DeepSeek API key.

    Priority: DEEPSEEK_API_KEY > ~/.aldebaran/intel_config.json.
    """
    return credential_store.load_value(_CONFIG_FILE, 'deepseek_api_key', _ENV_VAR)


def _save_config_value(field: str, value: str, label: str) -> bool:
    try:
        return credential_store.save_value(
            _CONFIG_FILE,
            field,
            value,
            log_errors=False,
            raise_errors=True,
        )
    except Exception:
        logger.warning('%s 保存失败', label, exc_info=True)
        return False


def save_api_key(key: str) -> bool:
    """Persist the DeepSeek API key. Passing an empty string clears it."""
    return _save_config_value('deepseek_api_key', key, 'DeepSeek API Key')


def has_api_key() -> bool:
    """Return whether a DeepSeek API key is configured."""
    return bool(load_api_key())


def load_github_token() -> str:
    """Load an optional GitHub Personal Access Token."""
    return credential_store.load_value(_CONFIG_FILE, 'github_token', 'GITHUB_TOKEN')


def save_github_token(token: str) -> bool:
    """Persist the GitHub token. Passing an empty string clears it."""
    return _save_config_value('github_token', token, 'GitHub Token')


def load_tickflow_token() -> str:
    """Load the optional paid TickFlow market-data token.

    Priority: TICKFLOW_TOKEN > ~/.aldebaran/intel_config.json.
    """
    return credential_store.load_value(_CONFIG_FILE, 'tickflow_token', 'TICKFLOW_TOKEN')


def save_tickflow_token(token: str) -> bool:
    """Persist the paid TickFlow token. Passing an empty string clears it."""
    return _save_config_value('tickflow_token', token, 'TickFlow Token')


def has_tickflow_token() -> bool:
    """Return whether a TickFlow token is configured."""
    return bool(load_tickflow_token())


def load_tickflow_free_token() -> str:
    """Load the optional free TickFlow node token.

    Priority: TICKFLOW_FREE_TOKEN > ~/.aldebaran/intel_config.json.
    """
    return credential_store.load_value(
        _CONFIG_FILE,
        'tickflow_free_token',
        'TICKFLOW_FREE_TOKEN',
    )


def save_tickflow_free_token(token: str) -> bool:
    """Persist the free TickFlow node token. Passing an empty string clears it."""
    return _save_config_value('tickflow_free_token', token, 'TickFlow Free Token')


def enable_secure_mode() -> credential_store.StoreResult:
    """Switch local credential fields to Windows DPAPI storage."""
    return credential_store.enable_secure_mode(_CONFIG_FILE)


def disable_secure_mode() -> credential_store.StoreResult:
    """Switch local credential fields back to plaintext JSON storage."""
    return credential_store.disable_secure_mode(_CONFIG_FILE)


def is_secure_mode() -> bool:
    """Return whether local credential storage is currently DPAPI-backed."""
    return credential_store.is_secure_mode(_CONFIG_FILE)
