"""宏观状态机 — 持久化世界快照，供 AI 分析和预测使用。

落盘位置：BASE_DIR/macro_state.json
更新逻辑：仅走 deepseek-v4-flash，每字段 4 小时冷却；Pro 输出不写回此文件。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

from core.paths import HOME
_MACRO_FILE = HOME / 'macro_state.json'
_DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
_FLASH_MODEL = 'deepseek-v4-flash'
_REQUEST_TIMEOUT = 30
_FIELD_COOLDOWN = 4 * 3600  # 同一字段 4 小时内不重复更新

# 字段枚举值，供 AI 参考
_FIELD_SCHEMA = {
    'fed_stance': ['hawkish', 'hawkish_pause', 'neutral', 'dovish_pivot', 'cutting'],
    'us_china': ['escalation', 'tension', 'tariff_truce', 'negotiation', 'easing'],
    'risk_appetite': ['risk_off', 'cautious', 'neutral', 'recovering', 'risk_on'],
    'domestic_policy': ['tightening', 'neutral', 'mild_stimulus', 'stimulus_on', 'strong_stimulus'],
    'a_share_cycle': ['bear_trend', 'bottoming', 'sideways', 'policy_driven_rebound', 'bull_trend'],
}

_SYSTEM_PROMPT = """\
你是一位宏观研究员，负责根据最新财经情报更新宏观状态机。
你需要分析给定的情报，判断是否需要更新以下任意字段，并以严格 JSON 格式输出。

字段枚举值：
- fed_stance: hawkish / hawkish_pause / neutral / dovish_pivot / cutting
- us_china: escalation / tension / tariff_truce / negotiation / easing
- risk_appetite: risk_off / cautious / neutral / recovering / risk_on
- domestic_policy: tightening / neutral / mild_stimulus / stimulus_on / strong_stimulus
- a_share_cycle: bear_trend / bottoming / sideways / policy_driven_rebound / bull_trend

输出格式（只输出需要更新的字段，不需要更新则输出空对象）：
{
  "updates": {
    "字段名": "新枚举值"
  },
  "update_reason": "一句话说明更新原因（20-60字）",
  "triggered_by_event_id": "情报ID或空字符串"
}

若情报对宏观状态无明显影响，输出 {"updates": {}, "update_reason": "", "triggered_by_event_id": ""}。
不要虚构，不要输出枚举值以外的值。\
"""


def load_macro_state() -> dict:
    """读取宏观状态机；不存在返回 {}。"""
    try:
        if _MACRO_FILE.exists():
            return json.loads(_MACRO_FILE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def save_macro_state(state: dict):
    """保存宏观状态机；失败静默。"""
    try:
        _MACRO_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MACRO_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def get_macro_context_text() -> str:
    """返回宏观背景描述段（用于 prompt 注入）；state 为空时返回空字符串。"""
    state = load_macro_state()
    if not state:
        return ''
    _labels = {
        'fed_stance': '美联储立场',
        'us_china': '中美关系',
        'risk_appetite': '风险偏好',
        'domestic_policy': '国内政策',
        'a_share_cycle': 'A股周期',
    }
    lines = ['## 宏观背景']
    for field, label in _labels.items():
        val = state.get(field)
        if val:
            lines.append(f'▶ {label}: {val}')
    reason = state.get('update_reason')
    updated = state.get('last_updated')
    if reason:
        lines.append(f'▶ 最近更新: {reason}（{updated or ""}）')
    if len(lines) <= 1:
        return ''
    return '\n'.join(lines)


def update_macro_state_from_intel(events: list, api_key: str) -> bool:
    """用最近 critical 情报更新宏观状态机（flash 模型）。

    Args:
        events: IntelEvent 列表（取 level=='critical' 的最近5条）
        api_key: DeepSeek API key

    Returns:
        True 表示有字段被更新，False 表示未更新或失败。
    """
    if not api_key or not events:
        return False

    # 只取 critical 情报，最多 5 条
    critical = [e for e in events if getattr(e, 'level', '') == 'critical'][:5]
    if not critical:
        return False

    state = load_macro_state()
    now_ts = time.time()
    field_cooldowns: dict = state.get('field_cooldowns', {})

    # 构建情报摘要
    event_lines = []
    last_event_id = ''
    for ev in critical:
        eid = getattr(ev, 'id', '')
        title = getattr(ev, 'title', '')
        interp = getattr(ev, 'interpretation', '') or ''
        event_lines.append(f'[{eid}] {title}')
        if interp:
            event_lines.append(f'  解读：{interp[:80]}')
        last_event_id = eid

    user_msg = (
        f'当前宏观状态：\n{json.dumps({k: state.get(k) for k in _FIELD_SCHEMA}, ensure_ascii=False)}\n\n'
        f'最新 critical 情报：\n' + '\n'.join(event_lines) +
        '\n\n请判断是否需要更新宏观状态机字段。'
    )

    payload = {
        'model': _FLASH_MODEL,
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': user_msg},
        ],
        'response_format': {'type': 'json_object'},
        'max_tokens': 512,
    }
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    # 最多重试 2 次
    result = None
    for _attempt in range(2):
        try:
            r = requests.post(_DEEPSEEK_URL, headers=headers, json=payload, timeout=_REQUEST_TIMEOUT)
            r.raise_for_status()
            content = r.json()['choices'][0]['message'].get('content', '')
            result = json.loads(content)
            break
        except (json.JSONDecodeError, KeyError):
            continue
        except Exception:
            return False

    if not result or not isinstance(result.get('updates'), dict):
        return False

    updates: dict = result['updates']
    reason: str = result.get('update_reason', '')
    event_id: str = result.get('triggered_by_event_id', last_event_id)

    updated_any = False
    for field, new_val in updates.items():
        if field not in _FIELD_SCHEMA:
            continue
        if new_val not in _FIELD_SCHEMA[field]:
            continue
        # 冷却检查
        last_update_ts = field_cooldowns.get(field, 0)
        if (now_ts - last_update_ts) < _FIELD_COOLDOWN:
            continue
        state[field] = new_val
        field_cooldowns[field] = now_ts
        updated_any = True

    if updated_any:
        state['last_updated'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        state['update_reason'] = reason
        state['triggered_by_event_id'] = event_id
        state['field_cooldowns'] = field_cooldowns
        save_macro_state(state)

    return updated_any
