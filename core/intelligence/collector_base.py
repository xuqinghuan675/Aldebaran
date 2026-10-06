"""Base collector contract for intelligence sources."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from core.intelligence.freshness import freshness_for_item
from core.intelligence.models import NormalizedIntelItem, RawIntelItem, SourceSpec


@dataclass
class CollectorResult:
    source_id: str
    raw_items: list[RawIntelItem] = field(default_factory=list)
    normalized_items: list[NormalizedIntelItem] = field(default_factory=list)
    ok: bool = True
    error: str = ''
    source_status: dict[str, Any] = field(default_factory=dict)


class Collector:
    source_id: str = ''

    def fetch(
        self,
        spec: SourceSpec,
        *,
        code: str = '',
        name: str = '',
        context: dict[str, Any] | None = None,
    ) -> list[RawIntelItem]:
        raise NotImplementedError

    def normalize(
        self,
        raw: RawIntelItem,
        *,
        code: str = '',
        name: str = '',
        context: dict[str, Any] | None = None,
    ) -> list[NormalizedIntelItem]:
        raise NotImplementedError


def run_collector(
    collector: Collector,
    spec: SourceSpec,
    *,
    code: str = '',
    name: str = '',
    context: dict[str, Any] | None = None,
) -> CollectorResult:
    try:
        raw_items = collector.fetch(spec, code=code, name=name, context=context)
        normalized: list[NormalizedIntelItem] = []
        for raw in raw_items:
            normalized.extend(collector.normalize(raw, code=code, name=name, context=context))
        for item in normalized:
            item.source_status = 'ok'
            item.freshness = freshness_for_item(item, spec)
        status = {
            'source_id': spec.source_id,
            'status': 'ok',
            'raw_count': len(raw_items),
            'normalized_count': len(normalized),
            'error': '',
        }
        return CollectorResult(
            source_id=spec.source_id,
            raw_items=list(raw_items),
            normalized_items=normalized,
            ok=True,
            source_status=status,
        )
    except Exception as exc:
        message = str(exc)
        return CollectorResult(
            source_id=spec.source_id,
            ok=False,
            error=message,
            source_status={
                'source_id': spec.source_id,
                'status': 'error',
                'raw_count': 0,
                'normalized_count': 0,
                'error': message,
            },
        )


def run_collectors(
    collectors: Iterable[Collector],
    specs: Iterable[SourceSpec],
    *,
    code: str = '',
    name: str = '',
    context: dict[str, Any] | None = None,
) -> list[CollectorResult]:
    spec_by_id = {spec.source_id: spec for spec in specs}
    results: list[CollectorResult] = []
    for collector in collectors:
        spec = spec_by_id.get(collector.source_id)
        if spec is None:
            continue
        results.append(run_collector(collector, spec, code=code, name=name, context=context))
    return results
