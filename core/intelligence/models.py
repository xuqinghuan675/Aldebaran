"""Small data contracts for intelligence sources and normalized evidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


VALID_METHODS = {
    'api',
    'rss',
    'requests',
    'scrapling_static',
    'scrapling_dynamic',
    'scrapling_stealth',
    'manual',
}

VALID_ACCESS_MODES = {
    'free',
    'optional_api',
}

VALID_TRUST_LEVELS = {
    'official',
    'primary',
    'secondary',
    'media',
    'unknown',
}


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    name: str
    layer: str
    method: str
    url: str = ''
    ttl_hours: int = 6
    rate_limit_seconds: int = 1
    enabled: bool = True
    parser: str = ''
    trust_level: str = 'unknown'
    form: str = ''
    access_mode: str = 'free'
    requires_api_key: bool = False
    credential_env: str = ''
    credential_aliases: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    supports_pdf_body: bool = False

    def __post_init__(self) -> None:
        if self.method not in VALID_METHODS:
            raise ValueError(f'invalid source method: {self.method}')
        if self.trust_level not in VALID_TRUST_LEVELS:
            raise ValueError(f'invalid trust level: {self.trust_level}')
        if self.access_mode not in VALID_ACCESS_MODES:
            raise ValueError(f'invalid access mode: {self.access_mode}')
        if int(self.ttl_hours) <= 0:
            raise ValueError('ttl_hours must be positive')

    @property
    def id(self) -> str:
        return self.source_id

    @property
    def ttl(self) -> int:
        return self.ttl_hours

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RawIntelItem:
    source_id: str
    source_name: str
    source_url: str
    fetched_at: str
    published_at: str | None
    title: str
    text: str
    html: str = ''
    url: str = ''
    layer: str = 'news_event'
    trust_level: str = 'unknown'
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NormalizedIntelItem:
    id: str
    source_id: str
    title: str
    summary: str
    layer: str
    direction: str
    related_codes: list[str]
    related_names: list[str]
    related_sectors: list[str]
    evidence_type: str
    published_at: str | None
    fetched_at: str
    url: str
    trust_level: str
    confidence: int
    source_url: str = ''
    category: str = ''
    relevance_reason: str = ''
    matched_keywords: list[str] = field(default_factory=list)
    metric_name: str = ''
    current_value: float | None = None
    unit: str = ''
    change_1d: float | None = None
    change_3d: float | None = None
    change_7d: float | None = None
    change_30d: float | None = None
    change_qoq: float | None = None
    change_yoy: float | None = None
    acceleration: str = 'unknown'
    expectation_gap: str = 'unknown'
    time_windows: list[str] = field(default_factory=list)
    relation_targets: list[str] = field(default_factory=list)
    source_status: str = 'ok'
    freshness: str = 'unknown'
    missing_evidence: list[str] = field(default_factory=list)
    raw_ref: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
