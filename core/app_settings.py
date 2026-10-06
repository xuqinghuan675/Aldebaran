from __future__ import annotations

import json
from typing import Any

from core.paths import HOME

_FILE = HOME / 'intel_config.json'


def load_setting(key: str, default: Any = None) -> Any:
    try:
        if _FILE.exists():
            return json.loads(_FILE.read_text(encoding='utf-8')).get(key, default)
    except Exception:
        pass
    return default


def save_setting(key: str, value: Any) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        cfg: dict = {}
        try:
            if _FILE.exists():
                cfg = json.loads(_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
        cfg[key] = value
        _FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass
