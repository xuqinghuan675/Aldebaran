"""intel_models.py — 事件情报数据模型。

IntelEvent 是面板唯一数据结构，由 intel_feed.json 反序列化而来。
所有字段均为字符串/列表/字典，便于 JSON 互转，无外部依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional


# ---------- 分类常量 ----------
CATEGORY_LABELS: dict[str, str] = {
    'macro':         '宏观流动性',
    'ai':            'AI科技',
    'semiconductor': '半导体',
    'new_energy':    '新能源',
    'robotics':      '机器人低空',
    'medical':       '医药',
    'consumer':      '消费',
    'military':      '军工',
    'gold':          '黄金有色',
    'finance':       '金融',
    'real_estate':   '地产',
    'policy':        '政策监管',
    'overseas':      '海外映射',
}

LEVEL_LABELS: dict[str, str] = {
    'info':      '关注',
    'important': '重要',
    'critical':  '关键',
}

DIRECTION_LABELS: dict[str, str] = {
    'bullish': '利好',
    'bearish': '利空',
    'neutral': '中性',
}

LEVEL_COLORS: dict[str, str] = {
    'info':      '#5dade2',
    'important': '#f0b429',
    'critical':  '#e94560',
}

DIRECTION_COLORS: dict[str, str] = {
    'bullish': '#e84040',
    'bearish': '#00c853',
    'neutral': '#888888',
}


# ---------- 事件数据类 ----------
@dataclass
class IntelEvent:
    id: str
    title: str
    category: str          # 见 CATEGORY_LABELS
    level: str             # info | important | critical
    direction: str         # bullish | bearish | neutral
    summary: str           # 一句话摘要（≤50字）
    interpretation: str    # 市场解读（≤120字）
    related_sectors: list[str] = field(default_factory=list)
    timestamp: str = ''    # "YYYY-MM-DD"
    source: str = ''       # 来源标注，非URL
    trading_tip: str = ''  # 操作提示（≤80字）

    # ---- 便捷属性 ----
    @property
    def date(self) -> Optional[date]:
        try:
            return datetime.strptime(self.timestamp, '%Y-%m-%d').date()
        except ValueError:
            return None

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def level_label(self) -> str:
        return LEVEL_LABELS.get(self.level, self.level)

    @property
    def level_color(self) -> str:
        return LEVEL_COLORS.get(self.level, '#888')

    @property
    def direction_label(self) -> str:
        return DIRECTION_LABELS.get(self.direction, self.direction)

    @property
    def direction_color(self) -> str:
        return DIRECTION_COLORS.get(self.direction, '#888')

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'title': self.title,
            'category': self.category,
            'level': self.level,
            'direction': self.direction,
            'summary': self.summary,
            'interpretation': self.interpretation,
            'related_sectors': self.related_sectors,
            'timestamp': self.timestamp,
            'source': self.source,
            'trading_tip': self.trading_tip,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'IntelEvent':
        return cls(
            id=str(d.get('id', '')),
            title=str(d.get('title', '')),
            category=str(d.get('category', 'macro')),
            level=str(d.get('level', 'info')),
            direction=str(d.get('direction', 'neutral')),
            summary=str(d.get('summary', '')),
            interpretation=str(d.get('interpretation', '')),
            related_sectors=list(d.get('related_sectors', [])),
            timestamp=str(d.get('timestamp', '')),
            source=str(d.get('source', '')),
            trading_tip=str(d.get('trading_tip', '')),
        )
