"""Collection plans that bind route profiles to catalog sources."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core.intelligence.models import SourceSpec
from core.intelligence.route_profile import RouteProfile
from core.intelligence.source_catalog import (
    CatalogSource,
    REQUIRED_FIELDS,
    get_source_catalog,
    has_source_credentials,
    specs_from_catalog_sources,
)


VALID_PLAN_MODES = ('efficiency', 'performance')


@dataclass(frozen=True)
class CollectionPlan:
    mode: str
    route_profile: RouteProfile
    sources: tuple[CatalogSource, ...]
    source_specs: tuple[SourceSpec, ...]
    skipped_sources: tuple[CatalogSource, ...]
    queries_by_source: dict[str, tuple[str, ...]]
    fields_by_source: dict[str, tuple[str, ...]]
    parse_disclosure_pdf: bool = False
    max_pdf_per_source: int = 0
    pdf_timeout_seconds: int = 8
    max_pdf_text_chars: int = 6000
    only_parse_when_title_matches: bool = True
    include_pdf_body: bool = False
    include_periodic_report: bool = False
    parse_periodic_report_pdf: bool = False
    max_periodic_pdf_per_source: int = 0
    periodic_pdf_timeout_seconds: int = 8
    max_periodic_text_chars: int = 30000

    @property
    def source_ids(self) -> tuple[str, ...]:
        return _unique(source.source_id for source in self.sources)

    def queries_for_source(self, source_id: str) -> tuple[str, ...]:
        return tuple(self.queries_by_source.get(source_id, ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            'mode': self.mode,
            'route_profile': self.route_profile.to_dict(),
            'sources': [source.to_dict() for source in self.sources],
            'source_ids': list(self.source_ids),
            'source_specs': [spec.to_dict() for spec in self.source_specs],
            'skipped_sources': [source.to_dict() for source in self.skipped_sources],
            'queries_by_source': {key: list(value) for key, value in self.queries_by_source.items()},
            'fields_by_source': {key: list(value) for key, value in self.fields_by_source.items()},
            'parse_disclosure_pdf': self.parse_disclosure_pdf,
            'max_pdf_per_source': self.max_pdf_per_source,
            'pdf_timeout_seconds': self.pdf_timeout_seconds,
            'max_pdf_text_chars': self.max_pdf_text_chars,
            'only_parse_when_title_matches': self.only_parse_when_title_matches,
            'include_pdf_body': self.include_pdf_body,
            'include_periodic_report': self.include_periodic_report,
            'parse_periodic_report_pdf': self.parse_periodic_report_pdf,
            'max_periodic_pdf_per_source': self.max_periodic_pdf_per_source,
            'periodic_pdf_timeout_seconds': self.periodic_pdf_timeout_seconds,
            'max_periodic_text_chars': self.max_periodic_text_chars,
            'pdf_control': {
                'parse_disclosure_pdf': self.parse_disclosure_pdf,
                'max_pdf_per_source': self.max_pdf_per_source,
                'pdf_timeout_seconds': self.pdf_timeout_seconds,
                'max_pdf_text_chars': self.max_pdf_text_chars,
                'only_parse_when_title_matches': self.only_parse_when_title_matches,
            },
            'periodic_report_control': {
                'parse_periodic_report_pdf': self.parse_periodic_report_pdf,
                'max_periodic_pdf_per_source': self.max_periodic_pdf_per_source,
                'periodic_pdf_timeout_seconds': self.periodic_pdf_timeout_seconds,
                'max_periodic_text_chars': self.max_periodic_text_chars,
            },
        }


def build_collection_plan(
    route_profile: RouteProfile,
    catalog: dict[str, tuple[CatalogSource, ...]] | None = None,
    *,
    mode: str = 'efficiency',
    include_fields: tuple[str, ...] | list[str] | None = None,
    include_pdf_body: bool = False,
    parse_disclosure_pdf: bool = False,
    max_pdf_per_source: int | None = None,
    pdf_timeout_seconds: int = 8,
    max_pdf_text_chars: int = 6000,
    only_parse_when_title_matches: bool = True,
    include_periodic_report: bool = False,
    parse_periodic_report_pdf: bool = False,
    max_periodic_pdf_per_source: int | None = None,
    periodic_pdf_timeout_seconds: int = 8,
    max_periodic_text_chars: int = 30000,
) -> CollectionPlan:
    if mode not in VALID_PLAN_MODES:
        raise ValueError(f'invalid collection mode: {mode}')

    catalog = catalog or get_source_catalog()
    fields = tuple(include_fields or REQUIRED_FIELDS)
    sources = _select_sources(catalog, fields, mode)
    runnable = tuple(
        source for source in sources
        if source.enabled_by_default and (source.access_mode == 'free' or has_source_credentials(source))
    )
    skipped = tuple(
        source for source in sources
        if source.enabled_by_default and source.access_mode == 'optional_api' and not has_source_credentials(source)
    )
    fields_by_source = _fields_by_source(sources)
    queries_by_source = _queries_by_source(route_profile, fields_by_source)
    pdf_enabled = bool(parse_disclosure_pdf or include_pdf_body)
    periodic_enabled = bool(parse_periodic_report_pdf or include_periodic_report)
    return CollectionPlan(
        mode=mode,
        route_profile=route_profile,
        sources=sources,
        source_specs=specs_from_catalog_sources(runnable),
        skipped_sources=skipped,
        queries_by_source=queries_by_source,
        fields_by_source=fields_by_source,
        parse_disclosure_pdf=pdf_enabled,
        max_pdf_per_source=_pdf_limit(mode, pdf_enabled, max_pdf_per_source),
        pdf_timeout_seconds=_bounded_int(pdf_timeout_seconds, default=8, low=1, high=30),
        max_pdf_text_chars=_bounded_int(max_pdf_text_chars, default=6000, low=1000, high=100000),
        only_parse_when_title_matches=bool(only_parse_when_title_matches),
        include_pdf_body=bool(include_pdf_body),
        include_periodic_report=bool(include_periodic_report),
        parse_periodic_report_pdf=periodic_enabled,
        max_periodic_pdf_per_source=_periodic_pdf_limit(periodic_enabled, max_periodic_pdf_per_source),
        periodic_pdf_timeout_seconds=_bounded_int(periodic_pdf_timeout_seconds, default=8, low=1, high=30),
        max_periodic_text_chars=_bounded_int(max_periodic_text_chars, default=30000, low=1000, high=200000),
    )


def _select_sources(
    catalog: dict[str, tuple[CatalogSource, ...]],
    fields: tuple[str, ...],
    mode: str,
) -> tuple[CatalogSource, ...]:
    selected: list[CatalogSource] = []
    for field in fields:
        field_sources = list(catalog.get(field) or ())
        free_sources = [source for source in field_sources if source.access_mode == 'free']
        optional_sources = [
            source for source in field_sources
            if source.access_mode == 'optional_api' and source.enabled_by_default
        ]
        if mode == 'efficiency':
            selected.extend(_efficiency_sources(field, free_sources))
            selected.extend(optional_sources[:1])
        else:
            selected.extend(free_sources)
            selected.extend(optional_sources)
    return _dedupe_by_field(selected)


def _efficiency_sources(field: str, sources: list[CatalogSource]) -> list[CatalogSource]:
    if not sources:
        return []
    if field in {'company_disclosure', 'order_contract', 'risk'}:
        preferred = [source for source in sources if source.source_id == 'requests:company_announcements']
        return preferred or sources[:1]
    if field in {'cost', 'demand', 'export', 'customer_supplier', 'inventory', 'capacity', 'competition'}:
        preferred = [
            source for source in sources
            if source.source_id in {'existing:thin_layer_context', 'requests:industry_chain_pages', 'requests:company_announcements'}
        ]
        return preferred[:2] or sources[:1]
    return sources[:2]


def _queries_by_source(
    route_profile: RouteProfile,
    fields_by_source: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    queries: dict[str, tuple[str, ...]] = {}
    for source_id, fields in fields_by_source.items():
        values: list[str] = []
        if source_id == 'requests:company_announcements':
            values.extend([route_profile.name, route_profile.code])
        for field in fields:
            values.extend(route_profile.queries_for_field(field))
        if not values:
            values.extend(route_profile.keywords[:3])
        queries[source_id] = _unique(values)[:10]
    return queries


def _fields_by_source(sources: tuple[CatalogSource, ...]) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    for source in sources:
        values = fields.setdefault(source.source_id, [])
        if source.field not in values:
            values.append(source.field)
    return {source_id: tuple(values) for source_id, values in fields.items()}


def _dedupe_by_field(sources: list[CatalogSource]) -> tuple[CatalogSource, ...]:
    out: list[CatalogSource] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        key = (source.source_id, source.field)
        if key in seen:
            continue
        seen.add(key)
        out.append(source)
    return tuple(out)


def _unique(values) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = str(value or '').strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _pdf_limit(mode: str, enabled: bool, value: int | None) -> int:
    if not enabled:
        return 0
    if value is not None:
        return _bounded_int(value, default=0, low=0, high=10)
    return 3 if mode == 'performance' else 1


def _periodic_pdf_limit(enabled: bool, value: int | None) -> int:
    if not enabled:
        return 0
    if value is not None:
        return _bounded_int(value, default=0, low=0, high=1)
    return 1


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))
