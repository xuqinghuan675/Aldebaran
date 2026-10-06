"""用户画像管理 — Aldebaran v3.0 个性化分析基石。

配置文件：BASE_DIR/user_profile.json

画像驱动个性化分析输出，让 AI 贴合用户真实风格。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.paths import HOME
_PROFILE_FILE = HOME / 'user_profile.json'

# ---------- 枚举常量 ----------

EXPERIENCE_OPTIONS = ['<1年', '1-3年', '3-5年', '5-10年', '10年+']
CAPITAL_OPTIONS = ['<5万', '5-10万', '10-50万', '50-100万', '100万+']
HORIZON_OPTIONS = ['短线（1-5天）', '中线（1-4周）', '长线（1-12月）', '混合']
LOSS_REACTION_OPTIONS = ['立刻止损', '等反弹', '加仓摊薄', '不操作观望']
PRIMARY_GOAL_OPTIONS = ['稳健增值', '激进收益', '学习练手', '资产配置']

AI_PERSONAS = {
    '军师型': '冷静谋略派。先给全局再给战术。重视风险/胜率比。语气克制，引用历史案例。',
    '学者型': '严谨数据派。每个判断必引指标/数据。语气中性，会自我修正。',
    '交易员型': '快节奏实战派。关注短期资金和情绪。语气直接，重点给买卖点。',
    '佛系型': '价值长期派。强调护城河和估值。语气平和，劝阻追涨杀跌。',
    '博弈型': '机构对手盘视角。分析主力意图和散户心理。语气犀利，揭示陷阱。',
}

# Agent 权重 — 不同 horizon 下的默认值（macro/user/company/technical 和为 1.0）
DEFAULT_AGENT_WEIGHTS = {
    '短线（1-5天）': {'macro': 0.15, 'user': 0.10, 'company': 0.10, 'technical': 0.65},
    '中线（1-4周）': {'macro': 0.25, 'user': 0.15, 'company': 0.25, 'technical': 0.35},
    '长线（1-12月）': {'macro': 0.30, 'user': 0.15, 'company': 0.40, 'technical': 0.15},
    '混合': {'macro': 0.25, 'user': 0.15, 'company': 0.25, 'technical': 0.35},
}


def _default_profile() -> dict[str, Any]:
    """空画像骨架，供首次填写参考。"""
    return {
        'experience': '',
        'capital_range': '',
        'risk_tolerance': {
            'max_drawdown': 15,
            'loss_reaction': 'wait',
            'primary_goal': 'stable_growth',
        },
        'style': {
            'horizon': '中线（1-4周）',
            'preferred_sectors': [],
            'forbidden': [],
        },
        'narrative': {
            'best_trade': '',
            'worst_trade': '',
        },
        'ai_persona': '军师型',
        'agent_weights': DEFAULT_AGENT_WEIGHTS['中线（1-4周）'].copy(),
        'position_limits': {'max_single_stock_pct': 20},
        'completed': False,
        'created_at': '',
        'updated_at': '',
    }


# ---------- CRUD ----------

def load_profile() -> dict[str, Any]:
    """读取画像；不存在或损坏时返回空骨架。"""
    if not _PROFILE_FILE.exists():
        return _default_profile()
    try:
        data = json.loads(_PROFILE_FILE.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return _default_profile()
        # 兼容字段补全
        base = _default_profile()
        for k, v in base.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _default_profile()


def save_profile(profile: dict[str, Any]) -> None:
    """持久化画像。stable key 顺序以保证 DeepSeek caching 命中率。"""
    try:
        _PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 使用 sort_keys 保证序列化稳定（caching 命中前提）
        _PROFILE_FILE.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8',
        )
    except Exception:
        pass


# ---------- Agent 权重 ----------

def get_agent_weights(profile: dict[str, Any] | None = None) -> dict[str, float]:
    """获取当前画像的 Agent 权重。

    优先级：profile.agent_weights > horizon 默认值
    """
    if profile is None:
        profile = load_profile()
    weights = profile.get('agent_weights')
    if isinstance(weights, dict) and all(k in weights for k in ('macro', 'user', 'company', 'technical')):
        return weights
    horizon = profile.get('style', {}).get('horizon', '中线（1-4周）')
    return DEFAULT_AGENT_WEIGHTS.get(horizon, DEFAULT_AGENT_WEIGHTS['中线（1-4周）']).copy()


def update_agent_weights(weights: dict[str, float]) -> None:
    """用户在 UI 调整权重时调用。"""
    profile = load_profile()
    profile['agent_weights'] = weights
    save_profile(profile)


# ---------- Prompt 注入 ----------

def build_profile_summary(profile: dict[str, Any] | None = None) -> str:
    """生成 prompt-ready 文本，注入到 G 层决策。

    输出固定结构以保证 DeepSeek caching 命中。
    """
    if profile is None:
        profile = load_profile()
    persona = profile.get('ai_persona', '军师型')
    persona_desc = AI_PERSONAS.get(persona, '')

    risk = profile.get('risk_tolerance', {})
    style = profile.get('style', {})
    narrative = profile.get('narrative', {})
    limits = profile.get('position_limits', {})

    parts = [
        f'【用户画像】',
        f'  - 投资经验：{profile.get("experience", "未填")}',
        f'  - 资金规模：{profile.get("capital_range", "未填")}',
        f'  - 可接受最大回撤：{risk.get("max_drawdown", 15)}%',
        f'  - 套牢反应：{risk.get("loss_reaction", "等反弹")}',
        f'  - 投资目标：{risk.get("primary_goal", "稳健增值")}',
        f'  - 持仓周期偏好：{style.get("horizon", "中线")}',
        f'  - 偏好板块：{", ".join(style.get("preferred_sectors", [])) or "无特定偏好"}',
        f'  - 禁区：{", ".join(style.get("forbidden", [])) or "无"}',
        f'  - 单股仓位上限：{limits.get("max_single_stock_pct", 20)}%',
        f'  - AI 风格人格：{persona}（{persona_desc}）',
    ]

    best = narrative.get('best_trade', '').strip()
    worst = narrative.get('worst_trade', '').strip()
    if best:
        parts.append(f'  - 最成功交易：{best[:120]}')
    if worst:
        parts.append(f'  - 最失败交易：{worst[:120]}')

    return '\n'.join(parts)


__all__ = [
    'EXPERIENCE_OPTIONS', 'CAPITAL_OPTIONS', 'HORIZON_OPTIONS',
    'LOSS_REACTION_OPTIONS', 'PRIMARY_GOAL_OPTIONS',
    'AI_PERSONAS', 'DEFAULT_AGENT_WEIGHTS',
    'load_profile', 'save_profile',
    'get_agent_weights', 'update_agent_weights',
    'build_profile_summary',
]
