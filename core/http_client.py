"""Small wrapper for Aldebaran-owned HTTP calls."""
from __future__ import annotations

import logging
from typing import Any, Mapping

import requests
from requests.structures import CaseInsensitiveDict

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
DEFAULT_TRUST_ENV = True
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)


def _merged_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    merged = CaseInsensitiveDict({'User-Agent': DEFAULT_USER_AGENT})
    if headers:
        merged.update(headers)
    return dict(merged)


def session(
    *,
    headers: Mapping[str, str] | None = None,
    trust_env: bool | None = None,
) -> requests.Session:
    s = requests.Session()
    s.headers.update(_merged_headers(headers))
    s.trust_env = DEFAULT_TRUST_ENV if trust_env is None else trust_env
    return s


def request(method: str, url: str, **kwargs: Any) -> requests.Response:
    trust_env = kwargs.pop('trust_env', None)
    headers = kwargs.pop('headers', None)
    if 'timeout' not in kwargs:
        kwargs['timeout'] = DEFAULT_TIMEOUT
    kwargs['headers'] = _merged_headers(headers)

    method_upper = method.upper()
    s = session(trust_env=trust_env)
    try:
        return s.request(method_upper, url, **kwargs)
    except requests.RequestException as exc:
        logger.debug('HTTP %s %s failed: %s', method_upper, url, exc)
        raise


def get(url: str, **kwargs: Any) -> requests.Response:
    return request('GET', url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request('POST', url, **kwargs)
