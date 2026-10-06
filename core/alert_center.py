"""AlertCenter — 异动提醒核心模块。

职责：
- 管理提醒配置（alert_settings.json）
- 冷却去重（alert_cooldown.json，key 带日期）
- 写提醒日志（alert_log.json，保留 10 天）
- 发出 alert_triggered 信号供 UI 消费

提醒事件统一结构：
    {'type': str, 'title': str, 'message': str,
     'level': 'info'|'warning'|'critical',
     'key': str, 'payload': dict, 'timestamp': str}

冷却 key 示例：2026-05-11:watchlist_pct:300308:up:5
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.constants import CACHE_DIR


# ---- 涨停阈值判断 ----
def get_limit_threshold(code: str, name: str = '') -> float:
    """根据股票代码前缀和名称判断涨停/跌停阈值（百分比）。

    ST/*ST → 5%，主板 → 10%，创业板/科创板 → 20%，北交所 → 30%。
    无法识别时返回 10%（保守默认值）。
    """
    c = str(code).strip()
    n = str(name).strip()
    # ST 判断（名称中包含 ST）
    if 'ST' in n.upper():
        return 5.0
    # 纯 6 位代码
    if len(c) >= 6:
        prefix = c[:3] if len(c) == 6 else c[2:5] if len(c) == 8 else ''
        if not prefix:
            return 10.0
        # 北交所：4/8 开头
        if prefix.startswith(('4', '8')) and not prefix.startswith('80'):
            return 30.0
        # 科创板：688
        if prefix.startswith('688'):
            return 20.0
        # 创业板：300/301
        if prefix.startswith(('300', '301')):
            return 20.0
        # 主板
        if prefix.startswith(('6', '0')):
            return 10.0
    return 10.0


def is_limit_up(code: str, name: str, pct: float) -> bool:
    """判断是否涨停。"""
    threshold = get_limit_threshold(code, name)
    return pct >= threshold - 0.5  # 容差 0.5%


# ---- 默认配置 ----
_DEFAULT_SETTINGS = {
    'enabled': True,
    'desktop_notification': True,
    'sound': False,
    # 自选股涨跌幅
    'watchlist_pct_enabled': True,
    'watchlist_pct_thresholds': [5, 8, 10],
    'watchlist_pct_cooldown': 600,  # 秒
    # 新股炸板/回封
    'ipo_explode_enabled': True,
    'ipo_reseal_enabled': True,
    'ipo_weak_seal_enabled': False,
    'ipo_weak_seal_threshold': 30000,  # 手
    # 板块 TOP3 突破
    'sector_top3_enabled': True,
    'sector_top3_min_net': 3.0,  # 亿
    'sector_top3_cooldown': 900,  # 秒
    'open_silence_minutes': 5,   # 每交易时段头N分钟跳过TOP3提醒（避免开盘集中爆发）
    # 自定义弹窗
    'custom_popup_enabled': False,
    'popup_size': 'medium',   # 'small'|'medium'|'large'
    'popup_duration': 5,      # 秒 3–30
    # 资金共振
    'resonance_enabled': True,
    'resonance_min_rank': 3,
    'resonance_min_net': 5.0,  # 亿
    'resonance_min_zt': 5,
    'resonance_cooldown': 300,  # 秒
}

_SETTINGS_FILE = CACHE_DIR / 'alert_settings.json'
_COOLDOWN_FILE = CACHE_DIR / 'alert_cooldown.json'
_LOG_FILE = CACHE_DIR / 'alert_log.json'
_LOG_MAX_DAYS = 10


class AlertCenter(QObject):
    """提醒中心：配置、冷却、日志、信号。"""

    alert_triggered = Signal(dict)  # 新提醒事件

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings = self._load_settings()
        self._cooldown = self._load_cooldown()
        self._log: list[dict] = self._load_log()

    # ---- 配置 ----
    def get_setting(self, key, default=None):
        return self._settings.get(key, _DEFAULT_SETTINGS.get(key, default))

    def set_setting(self, key, value):
        self._settings[key] = value
        self._save_settings()

    def get_all_settings(self) -> dict:
        merged = dict(_DEFAULT_SETTINGS)
        merged.update(self._settings)
        return merged

    def update_settings(self, updates: dict):
        self._settings.update(updates)
        self._save_settings()

    def _load_settings(self) -> dict:
        try:
            if _SETTINGS_FILE.exists():
                data = json.loads(_SETTINGS_FILE.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return dict(_DEFAULT_SETTINGS)

    def _save_settings(self):
        try:
            _SETTINGS_FILE.write_text(
                json.dumps(self._settings, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except Exception:
            pass

    # ---- 冷却去重 ----
    def is_cooled_down(self, key: str, cooldown_seconds: int = 600) -> bool:
        """检查 key 是否已冷却。True = 可以触发。"""
        last_ts = self._cooldown.get(key, 0)
        return (time.time() - last_ts) >= cooldown_seconds

    def mark_triggered(self, key: str):
        """标记 key 已触发（更新冷却时间戳）。"""
        self._cooldown[key] = time.time()
        self._save_cooldown()

    def _load_cooldown(self) -> dict:
        try:
            if _COOLDOWN_FILE.exists():
                data = json.loads(_COOLDOWN_FILE.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    # 清理过期条目（不是今天的 key）
                    today = datetime.now().strftime('%Y-%m-%d')
                    return {k: v for k, v in data.items() if k.startswith(today)}
        except Exception:
            pass
        return {}

    def _save_cooldown(self):
        try:
            _COOLDOWN_FILE.write_text(
                json.dumps(self._cooldown, ensure_ascii=False),
                encoding='utf-8',
            )
        except Exception:
            pass

    # ---- 日志 ----
    def get_log(self, days: int = 1) -> list[dict]:
        """获取最近 N 天的日志。"""
        if days <= 0:
            return list(self._log)
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        return [e for e in self._log if e.get('timestamp', '') >= cutoff]

    def clear_today_log(self):
        today = datetime.now().strftime('%Y-%m-%d')
        self._log = [e for e in self._log if not e.get('timestamp', '').startswith(today)]
        self._save_log()

    def _append_log(self, event: dict):
        self._log.append(event)
        # 清理超过 _LOG_MAX_DAYS 的旧日志
        cutoff = (datetime.now() - timedelta(days=_LOG_MAX_DAYS)).strftime('%Y-%m-%d')
        self._log = [e for e in self._log if e.get('timestamp', '') >= cutoff]
        self._save_log()

    def _load_log(self) -> list:
        try:
            if _LOG_FILE.exists():
                data = json.loads(_LOG_FILE.read_text(encoding='utf-8'))
                if isinstance(data, list):
                    cutoff = (datetime.now() - timedelta(days=_LOG_MAX_DAYS)).strftime('%Y-%m-%d')
                    return [e for e in data if e.get('timestamp', '') >= cutoff]
        except Exception:
            pass
        return []

    def _save_log(self):
        try:
            _LOG_FILE.write_text(
                json.dumps(self._log, ensure_ascii=False, indent=1),
                encoding='utf-8',
            )
        except Exception:
            pass

    # ---- 核心触发入口 ----
    def try_trigger(self, event_type: str, title: str, message: str,
                    level: str = 'info', key: str = '',
                    cooldown: int = 600, payload: dict | None = None) -> bool:
        """尝试触发一条提醒。

        如果全局关闭或冷却未到，返回 False。
        成功触发返回 True，同时写日志、发信号。
        """
        if not self.get_setting('enabled', True):
            return False

        # 构建完整 key（无日期前缀时自动加）
        today = datetime.now().strftime('%Y-%m-%d')
        full_key = key if key.startswith(today) else f'{today}:{key}'

        if not self.is_cooled_down(full_key, cooldown):
            return False

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        event = {
            'type': event_type,
            'title': title,
            'message': message,
            'level': level,
            'key': full_key,
            'payload': payload or {},
            'timestamp': now_str,
        }
        self.mark_triggered(full_key)
        self._append_log(event)
        self.alert_triggered.emit(event)
        return True
