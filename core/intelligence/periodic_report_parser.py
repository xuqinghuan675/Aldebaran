"""Periodic report text parser for intelligence evidence."""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.intelligence.models import NormalizedIntelItem


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[\u3002\uff01\uff1f!?;\uff1b])\s*|\n+')
_SPACE_RE = re.compile(r'\s+')
_MAX_EVIDENCE_CHARS = 420


@dataclass(frozen=True)
class PeriodicReportEvidence:
    field: str
    layer: str
    category: str
    title: str
    summary: str
    evidence_text: str
    matched_keywords: list[str] = field(default_factory=list)
    confidence: int = 0
    report_type: str = 'unknown'
    section: str = ''
    source_url: str = ''
    published_at: str | None = None
    raw_ref: str = ''
    code: str = ''
    name: str = ''
    page: int = 0
    table_header: list[str] = field(default_factory=list)
    table_quality_score: float = 0.0
    table_values: dict[str, Any] = field(default_factory=dict)
    table_row_index: int = 0


_SEMIANNUAL_TERMS = (
    '\u534a\u5e74\u5ea6\u62a5\u544a',
    '\u4e2d\u671f\u62a5\u544a',
    '\u534a\u5e74\u62a5',
    '\u4e2d\u62a5',
)
_QUARTERLY_TERMS = (
    '\u7b2c\u4e00\u5b63\u5ea6\u62a5\u544a',
    '\u7b2c\u4e09\u5b63\u5ea6\u62a5\u544a',
    '\u5b63\u5ea6\u62a5\u544a',
    '\u4e00\u5b63\u62a5',
    '\u4e09\u5b63\u62a5',
    'Q1',
    'Q3',
)
_ANNUAL_TERMS = (
    '\u5e74\u5ea6\u62a5\u544a',
    '\u5e74\u62a5',
)

_SECTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('main_business', (
        '\u4e3b\u8425\u4e1a\u52a1',
        '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210',
        '\u4e3b\u8981\u4e1a\u52a1',
        '\u4e3b\u8425\u4ea7\u54c1',
    )),
    ('management_discussion', (
        '\u7ecf\u8425\u60c5\u51b5\u8ba8\u8bba\u4e0e\u5206\u6790',
        '\u7ba1\u7406\u5c42\u8ba8\u8bba\u4e0e\u5206\u6790',
        '\u7ecf\u8425\u60c5\u51b5',
    )),
    ('financial_statements', (
        '\u8d22\u52a1\u62a5\u8868',
        '\u4e3b\u8981\u4f1a\u8ba1\u6570\u636e',
        '\u4e3b\u8981\u8d22\u52a1\u6307\u6807',
        '\u5408\u5e76\u8d44\u4ea7\u8d1f\u503a\u8868',
    )),
    ('customer_supplier', (
        '\u524d\u4e94\u5927\u5ba2\u6237',
        '\u524d\u4e94\u540d\u5ba2\u6237',
        '\u524d\u4e94\u5927\u4f9b\u5e94\u5546',
        '\u524d\u4e94\u540d\u4f9b\u5e94\u5546',
        '\u5ba2\u6237\u548c\u4f9b\u5e94\u5546',
    )),
    ('business_segments', (
        '\u5206\u884c\u4e1a',
        '\u5206\u4ea7\u54c1',
        '\u5206\u5730\u533a',
        '\u5206\u5730\u533a\u6536\u5165',
        '\u5883\u5916\u6536\u5165',
    )),
    ('inventory', (
        '\u5b58\u8d27',
        '\u5e94\u6536\u8d26\u6b3e',
        '\u6bdb\u5229\u7387',
    )),
    ('cost', (
        '\u6210\u672c\u6784\u6210',
        '\u8425\u4e1a\u6210\u672c',
        '\u539f\u6750\u6599',
    )),
    ('risk_factors', (
        '\u98ce\u9669\u56e0\u7d20',
        '\u7ecf\u8425\u98ce\u9669',
        '\u91cd\u5927\u98ce\u9669',
    )),
)

_FIELD_RULES: tuple[dict[str, Any], ...] = (
    {
        'field': 'financial',
        'layer': 'financial',
        'category': 'periodic_financial',
        'keywords': (
            '\u8425\u4e1a\u6536\u5165',
            '\u8425\u6536',
            '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165',
            '\u51c0\u5229\u6da6',
            '\u6bdb\u5229\u7387',
            '\u73b0\u91d1\u6d41',
            '\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d',
            '\u5e94\u6536\u8d26\u6b3e',
        ),
    },
    {
        'field': 'inventory',
        'layer': 'inventory',
        'category': 'inventory_ar_margin',
        'keywords': (
            '\u5b58\u8d27',
            '\u5e93\u5b58',
            '\u5b58\u8d27\u5468\u8f6c',
            '\u8dcc\u4ef7\u51c6\u5907',
            '\u53bb\u5e93',
            '\u8865\u5e93',
        ),
    },
    {
        'field': 'customer_supplier',
        'layer': 'customer_supplier',
        'category': 'top_customer_supplier',
        'keywords': (
            '\u524d\u4e94\u5927\u5ba2\u6237',
            '\u524d\u4e94\u540d\u5ba2\u6237',
            '\u524d\u4e94\u5927\u4f9b\u5e94\u5546',
            '\u524d\u4e94\u540d\u4f9b\u5e94\u5546',
            '\u5ba2\u6237\u96c6\u4e2d\u5ea6',
            '\u91c7\u8d2d\u96c6\u4e2d\u5ea6',
            '\u4f9b\u5e94\u5546\u96c6\u4e2d\u5ea6',
            '\u5ba2\u6237\u540d\u79f0',
            '\u4f9b\u5e94\u5546\u540d\u79f0',
            '\u9500\u552e\u989d',
            '\u91c7\u8d2d\u989d',
        ),
    },
    {
        'field': 'export',
        'layer': 'export',
        'category': 'region_revenue',
        'keywords': (
            '\u5883\u5916\u6536\u5165',
            '\u5883\u5916',
            '\u6d77\u5916\u6536\u5165',
            '\u6d77\u5916',
            '\u5206\u5730\u533a\u6536\u5165',
            '\u6d77\u5916\u4e1a\u52a1',
            '\u5916\u9500',
            '\u51fa\u53e3',
            '\u56fd\u9645\u5e02\u573a',
        ),
    },
    {
        'field': 'cost',
        'layer': 'cost',
        'category': 'cost_structure',
        'keywords': (
            '\u8425\u4e1a\u6210\u672c',
            '\u6210\u672c\u6784\u6210',
            '\u4e3b\u8425\u4e1a\u52a1\u6210\u672c',
            '\u539f\u6750\u6599',
            '\u539f\u6750\u6599\u6210\u672c',
            '\u4eba\u5de5',
            '\u5236\u9020\u8d39\u7528',
        ),
    },
    {
        'field': 'demand',
        'layer': 'demand',
        'category': 'periodic_demand',
        'keywords': (
            '\u4e3b\u8425\u4ea7\u54c1',
            '\u4ea7\u54c1\u9500\u552e',
            '\u9500\u552e\u91cf',
            '\u4ea7\u9500\u91cf',
            '\u8ba2\u5355',
            '\u4e0b\u6e38\u9700\u6c42',
            '\u9700\u6c42',
        ),
    },
    {
        'field': 'competition',
        'layer': 'competition',
        'category': 'competition_section',
        'keywords': (
            '\u884c\u4e1a\u7ade\u4e89',
            '\u7ade\u4e89\u683c\u5c40',
            '\u5e02\u573a\u4efd\u989d',
            '\u540c\u884c',
            '\u4ef7\u683c\u7ade\u4e89',
            '\u7ade\u4e89\u52a0\u5267',
        ),
    },
    {
        'field': 'risk',
        'layer': 'risk',
        'category': 'annual_report_risk',
        'keywords': (
            '\u98ce\u9669\u56e0\u7d20',
            '\u7ecf\u8425\u98ce\u9669',
            '\u6c47\u7387\u98ce\u9669',
            '\u8d38\u6613\u98ce\u9669',
            '\u5ba2\u6237\u96c6\u4e2d\u98ce\u9669',
            '\u98ce\u9669',
        ),
    },
)

_FIELD_RULE_BY_NAME = {str(rule['field']): rule for rule in _FIELD_RULES}

_TABLE_SECTION_BY_FIELD = {
    'financial': {'financial_statements', 'main_business', 'business_segments'},
    'business_segments': {'business_segments', 'main_business'},
    'cost': {'cost'},
    'inventory': {'inventory'},
    'customer_supplier': {'customer_supplier'},
    'customer_concentration': {'customer_supplier'},
    'supplier_concentration': {'customer_supplier'},
    'export': {'business_segments'},
    'demand': {'main_business', 'business_segments'},
    'competition': {'competition'},
    'risk': {'risk_factors'},
}

_TABLE_QUERY_HEADER_TERMS = (
    '\u67e5\u8be2\u5173\u952e\u8bcd',
    '\u80a1\u7968\u540d\u79f0',
    '\u80a1\u7968\u4ee3\u7801',
    '\u8bc1\u5238\u4ee3\u7801',
    '\u8bc1\u5238\u7b80\u79f0',
)

_MIN_TABLE_QUALITY_SCORE = 0.45

_TABLE_FIELD_CORE_TERMS = {
    'business_segments': (
        '\u5206\u884c\u4e1a',
        '\u5206\u4ea7\u54c1',
        '\u5206\u5730\u533a',
        '\u884c\u4e1a',
        '\u4ea7\u54c1',
        '\u5730\u533a',
        '\u8425\u4e1a\u6536\u5165',
        '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165',
        '\u8425\u4e1a\u6210\u672c',
        '\u6bdb\u5229\u7387',
    ),
    'export': (
        '\u5883\u5916',
        '\u6d77\u5916',
        '\u5916\u9500',
        '\u56fd\u5916',
        '\u51fa\u53e3',
        '\u5730\u533a',
        '\u8425\u4e1a\u6536\u5165',
    ),
    'inventory': (
        '\u5b58\u8d27',
        '\u539f\u6750\u6599',
        '\u5e93\u5b58\u5546\u54c1',
        '\u53d1\u51fa\u5546\u54c1',
        '\u8dcc\u4ef7\u51c6\u5907',
        '\u8d26\u9762\u4ef7\u503c',
        '\u8d26\u9762\u4f59\u989d',
    ),
    'cost': (
        '\u8425\u4e1a\u6210\u672c',
        '\u6210\u672c\u6784\u6210',
        '\u4e3b\u8425\u4e1a\u52a1\u6210\u672c',
        '\u6210\u672c\u9879\u76ee',
        '\u539f\u6750\u6599',
        '\u4eba\u5de5',
        '\u5236\u9020\u8d39\u7528',
        '\u8fd0\u8f93\u8d39',
    ),
    'customer_concentration': (
        '\u524d\u4e94\u5927\u5ba2\u6237',
        '\u524d\u4e94\u540d\u5ba2\u6237',
        '\u5ba2\u6237\u540d\u79f0',
        '\u5ba2\u6237',
        '\u9500\u552e\u989d',
        '\u9500\u552e\u91d1\u989d',
        '\u9500\u552e\u603b\u989d',
        '\u5360\u6bd4',
    ),
    'supplier_concentration': (
        '\u524d\u4e94\u5927\u4f9b\u5e94\u5546',
        '\u524d\u4e94\u540d\u4f9b\u5e94\u5546',
        '\u4f9b\u5e94\u5546\u540d\u79f0',
        '\u4f9b\u5e94\u5546',
        '\u91c7\u8d2d\u989d',
        '\u91c7\u8d2d\u91d1\u989d',
        '\u91c7\u8d2d\u603b\u989d',
        '\u5360\u6bd4',
    ),
    'financial': (
        '\u8425\u4e1a\u6536\u5165',
        '\u51c0\u5229\u6da6',
        '\u6bdb\u5229\u7387',
        '\u73b0\u91d1\u6d41',
    ),
}

_BUSINESS_SEGMENT_HEADER_TERMS = (
    '\u5206\u884c\u4e1a',
    '\u5206\u4ea7\u54c1',
    '\u5206\u5730\u533a',
    '\u884c\u4e1a',
    '\u4ea7\u54c1',
    '\u5730\u533a',
)
_SEGMENT_HEADER_ROW_TERMS = (
    '\u5206\u884c\u4e1a',
    '\u5206\u4ea7\u54c1',
    '\u5206\u5730\u533a',
    '\u5206\u9500\u552e\u6a21\u5f0f',
    '\u5206\u884c\u4e1a\u60c5\u51b5',
    '\u5206\u4ea7\u54c1\u60c5\u51b5',
    '\u5206\u5730\u533a\u60c5\u51b5',
    '\u5206\u9500\u552e\u6a21\u5f0f\u60c5\u51b5',
    '\u8425\u4e1a\u6536\u5165',
    '\u8425\u4e1a\u6210\u672c',
    '\u6bdb\u5229\u7387',
)

_EXPORT_ROW_TERMS = ('\u5883\u5916', '\u6d77\u5916', '\u5916\u9500', '\u56fd\u5916', '\u51fa\u53e3')
_INVENTORY_COMPONENT_TERMS = ('\u539f\u6750\u6599', '\u5e93\u5b58\u5546\u54c1', '\u53d1\u51fa\u5546\u54c1', '\u8dcc\u4ef7\u51c6\u5907', '\u5b58\u8d27')
_COST_COMPONENT_TERMS = ('\u539f\u6750\u6599', '\u4eba\u5de5\u6210\u672c', '\u4eba\u5de5', '\u5236\u9020\u8d39\u7528', '\u8fd0\u8f93\u8d39')
_GENERIC_FINANCIAL_HEADER_TERMS = (
    '\u8d44\u4ea7\u8d1f\u503a\u8868',
    '\u5229\u6da6\u8868',
    '\u73b0\u91d1\u6d41\u91cf\u8868',
    '\u6d41\u52a8\u8d44\u4ea7',
    '\u975e\u6d41\u52a8\u8d44\u4ea7',
    '\u8d44\u4ea7\u603b\u8ba1',
    '\u671f\u672b\u4f59\u989d',
    '\u671f\u521d\u4f59\u989d',
    '\u672c\u671f\u6570',
    '\u4e0a\u671f\u6570',
)

_TABLE_CLASSIFIER_RULES: tuple[dict[str, Any], ...] = (
    {
        'field': 'customer_supplier',
        'section_ids': {'customer_supplier'},
        'header_keywords': (
            '\u524d\u4e94\u540d\u5ba2\u6237',
            '\u524d\u4e94\u5927\u5ba2\u6237',
            '\u524d\u4e94\u540d\u4f9b\u5e94\u5546',
            '\u524d\u4e94\u5927\u4f9b\u5e94\u5546',
            '\u5ba2\u6237\u540d\u79f0',
            '\u4f9b\u5e94\u5546\u540d\u79f0',
            '\u9500\u552e\u989d',
            '\u91c7\u8d2d\u989d',
            '\u5360\u6bd4',
        ),
        'row_keywords': (
            '\u5ba2\u6237',
            '\u4f9b\u5e94\u5546',
            '\u9500\u552e\u989d',
            '\u91c7\u8d2d\u989d',
        ),
        'minimum_header_hits': 1,
    },
    {
        'field': 'inventory',
        'section_ids': {'inventory'},
        'header_keywords': (
            '\u5b58\u8d27',
            '\u539f\u6750\u6599',
            '\u5e93\u5b58\u5546\u54c1',
            '\u53d1\u51fa\u5546\u54c1',
            '\u8dcc\u4ef7\u51c6\u5907',
            '\u8d26\u9762\u4ef7\u503c',
        ),
        'row_keywords': (
            '\u5b58\u8d27',
            '\u539f\u6750\u6599',
            '\u5e93\u5b58\u5546\u54c1',
            '\u53d1\u51fa\u5546\u54c1',
            '\u8dcc\u4ef7\u51c6\u5907',
        ),
        'minimum_header_hits': 1,
    },
    {
        'field': 'cost',
        'section_ids': {'cost'},
        'header_keywords': (
            '\u8425\u4e1a\u6210\u672c',
            '\u6210\u672c\u6784\u6210',
            '\u6210\u672c\u9879\u76ee',
            '\u539f\u6750\u6599',
            '\u4eba\u5de5',
            '\u5236\u9020\u8d39\u7528',
            '\u8fd0\u8f93\u8d39',
            '\u5360\u6bd4',
        ),
        'row_keywords': (
            '\u8425\u4e1a\u6210\u672c',
            '\u539f\u6750\u6599',
            '\u4eba\u5de5',
            '\u5236\u9020\u8d39\u7528',
            '\u8fd0\u8f93\u8d39',
        ),
        'minimum_header_hits': 1,
    },
    {
        'field': 'export',
        'section_ids': {'business_segments'},
        'header_keywords': (
            '\u5206\u5730\u533a',
            '\u5730\u533a',
            '\u5883\u5185',
            '\u5883\u5916',
            '\u6d77\u5916',
            '\u5916\u9500',
            '\u5206\u4ea7\u54c1',
            '\u4ea7\u54c1',
            '\u5206\u884c\u4e1a',
            '\u884c\u4e1a',
            '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165',
            '\u8425\u4e1a\u6536\u5165',
            '\u6bdb\u5229\u7387',
        ),
        'row_keywords': (
            '\u5883\u5185',
            '\u5883\u5916',
            '\u6d77\u5916',
            '\u5916\u9500',
            '\u51fa\u53e3',
        ),
        'minimum_header_hits': 2,
    },
    {
        'field': 'financial',
        'section_ids': {'financial_statements', 'main_business', 'business_segments'},
        'header_keywords': (
            '\u8d44\u4ea7\u8d1f\u503a\u8868',
            '\u5229\u6da6\u8868',
            '\u73b0\u91d1\u6d41\u91cf\u8868',
            '\u8425\u4e1a\u6536\u5165',
            '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165',
            '\u51c0\u5229\u6da6',
            '\u7ecf\u8425\u73b0\u91d1\u6d41',
            '\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d',
            '\u6bdb\u5229\u7387',
        ),
        'row_keywords': (
            '\u8425\u4e1a\u6536\u5165',
            '\u51c0\u5229\u6da6',
            '\u7ecf\u8425\u73b0\u91d1\u6d41',
            '\u7ecf\u8425\u6d3b\u52a8\u4ea7\u751f\u7684\u73b0\u91d1\u6d41\u91cf\u51c0\u989d',
        ),
        'minimum_header_hits': 1,
    },
)

_TARGET_PERIODIC_FIELDS = (
    'financial',
    'business_segments',
    'cost',
    'inventory',
    'customer_supplier',
    'export',
    'demand',
    'competition',
    'risk',
)

_FORBIDDEN_TERMS = (
    '\u004b\u7ebf',
    '\u5747\u7ebf',
    ''.join(('M', 'A', 'C', 'D')),
    ''.join(('R', 'S', 'I')),
    ''.join(('K', 'D', 'J')),
    '\u652f\u6491\u4f4d',
    '\u538b\u529b\u4f4d',
    '\u4e70\u70b9',
    '\u6b62\u635f',
    '\u89e6\u53d1\u4ef7',
    '\u5931\u6548\u4ef7',
)


def detect_report_type(title: str, text: str) -> str:
    sample = _clean_text(f'{title} {str(text or "")[:1200]}')
    if _has_any_keyword(sample, _SEMIANNUAL_TERMS):
        return 'semiannual'
    if _has_any_keyword(sample, _QUARTERLY_TERMS):
        return 'quarterly'
    if _has_any_keyword(sample, _ANNUAL_TERMS):
        return 'annual'
    return 'unknown'


def split_report_sections(text: str) -> dict[str, str]:
    cleaned = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = [_clean_text(line) for line in cleaned.split('\n') if _clean_text(line)]
    if not lines:
        return {}

    sections: dict[str, str] = {}
    current = 'full_text'
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            sections[current] = _clean_text(' '.join(buffer))

    for line in lines:
        section = _section_for_heading(line)
        if section and _looks_like_heading(line):
            flush()
            current = section
            buffer = [line]
            continue
        buffer.append(line)
    flush()

    if not sections:
        return {'full_text': _clean_text(cleaned)}
    return sections


def build_periodic_report_section_index(text: str) -> list[dict[str, Any]]:
    cleaned = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    if not _clean_text(cleaned):
        return []

    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    offset = 0
    for raw_line in cleaned.splitlines(keepends=True):
        line_text = raw_line.rstrip('\n')
        stripped = _clean_text(line_text)
        section_id = _section_for_heading(stripped)
        if section_id and _looks_like_heading(stripped):
            if current is not None:
                current['end_char'] = offset
                current['text'] = cleaned[int(current['start_char']):offset].strip()
                sections.append(current)
            current = {
                'section_id': section_id,
                'title': stripped,
                'start_char': offset,
                'end_char': offset + len(raw_line),
                'text': stripped,
                'matched_keywords': _section_keywords_for_heading(stripped, section_id),
            }
        offset += len(raw_line)

    if current is not None:
        current['end_char'] = len(cleaned)
        current['text'] = cleaned[int(current['start_char']):].strip()
        sections.append(current)
    return sections


def parse_periodic_report_text(
    text: str,
    *,
    title: str = '',
    source_url: str = '',
    published_at: str | None = None,
    code: str = '',
    name: str = '',
    sections: list[dict[str, Any]] | None = None,
    table_blocks: list[dict[str, Any]] | None = None,
) -> list[PeriodicReportEvidence]:
    body = _clean_text(text)
    clean_title = _clean_text(title)
    tables = [_normalise_table_block(table) for table in (table_blocks or [])]
    if not body and not tables:
        return []

    report_type = detect_report_type(clean_title, f'{body} {_tables_searchable_text(tables)}')
    report_sections = _section_texts_from_index(sections) if sections else (split_report_sections(text) if body else {})
    evidence: list[PeriodicReportEvidence] = []
    seen: set[tuple[str, str]] = set()

    for table in tables:
        _append_table_evidence(
            evidence,
            seen,
            table,
            report_type=report_type,
            clean_title=clean_title,
            source_url=source_url,
            published_at=published_at,
            code=code,
            name=name,
        )

    for section, section_text in report_sections.items():
        searchable = _searchable_text(section_text)
        if not searchable:
            continue
        for rule in _FIELD_RULES:
            matched = [
                keyword
                for keyword in rule['keywords']
                if keyword and keyword in searchable
            ]
            if not matched:
                continue

            snippets = extract_periodic_report_sentences(section_text, matched, max_sentences=3)
            if not snippets:
                continue
            evidence_text = _truncate_evidence('\n'.join(snippets))
            key = (str(rule['field']), evidence_text)
            if key in seen:
                continue
            seen.add(key)

            field_name = str(rule['field'])
            category = _category_for_rule(rule, section, searchable)
            raw_ref = _raw_ref(field_name, report_type, section, evidence_text, source_url)
            evidence.append(PeriodicReportEvidence(
                field=field_name,
                layer=str(rule['layer']),
                category=category,
                title=clean_title or _default_title(field_name, report_type),
                summary=evidence_text[:260],
                evidence_text=evidence_text,
                matched_keywords=matched,
                confidence=min(9, 5 + len(matched)),
                report_type=report_type,
                section=section,
                source_url=str(source_url or ''),
                published_at=published_at,
                raw_ref=raw_ref,
                code=_clean_code(code),
                name=_clean_text(name),
            ))
    return _sort_periodic_evidence(evidence)


def periodic_report_field_missing_reasons(
    evidence: list[PeriodicReportEvidence],
    *,
    sections: list[dict[str, Any]] | None = None,
    table_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    emitted = {str(item.field or '') for item in evidence}
    if {'customer_concentration', 'supplier_concentration'} & emitted:
        emitted.add('customer_supplier')
    section_ids = {
        str(section.get('section_id') or '')
        for section in (sections or [])
        if isinstance(section, dict) and str(section.get('section_id') or '')
    }
    tables = [_normalise_table_block(table) for table in (table_blocks or [])]
    reasons: dict[str, str] = {}
    for field_name in _TARGET_PERIODIC_FIELDS:
        if field_name in emitted:
            continue
        reason = _table_missing_reason_for_field(field_name, tables)
        if reason:
            reasons[field_name] = reason
            continue
        expected_sections = _TABLE_SECTION_BY_FIELD.get(field_name, set())
        if expected_sections & section_ids:
            reasons[field_name] = 'section_without_matching_table'
            continue
        if not (section_ids or tables):
            reasons[field_name] = 'no_matching_table_or_section'
            continue
        reasons[field_name] = 'no_matching_table_or_section'
    return reasons


def periodic_report_table_status(
    evidence: list[PeriodicReportEvidence],
    *,
    table_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    table_evidence = [
        item for item in evidence
        if int(item.page or 0) > 0 or bool(item.table_header)
    ]
    by_field = Counter(str(item.field or '') for item in table_evidence if str(item.field or ''))
    tables = [_normalise_table_block(table) for table in (table_blocks or [])]
    low_quality = sum(
        1 for table in tables
        if (table.get('header') or table.get('rows'))
        and _table_quality_score(table) < _MIN_TABLE_QUALITY_SCORE
    )
    return {
        'table_evidence_count_by_field': dict(sorted(by_field.items())),
        'table_evidence_top_fields': [
            field for field, _count in by_field.most_common(6)
        ],
        'low_quality_table_skipped': int(low_quality),
        'customer_supplier_split_status': {
            'customer_concentration': int(by_field.get('customer_concentration', 0)),
            'supplier_concentration': int(by_field.get('supplier_concentration', 0)),
            'legacy_customer_supplier': int(by_field.get('customer_supplier', 0)),
        },
    }


def extract_periodic_report_sentences(
    text: str,
    keywords: list[str] | tuple[str, ...],
    max_sentences: int = 3,
) -> list[str]:
    wanted = [str(keyword or '').strip() for keyword in keywords if str(keyword or '').strip()]
    if not wanted or int(max_sentences or 0) <= 0:
        return []
    out: list[str] = []
    for sentence in _sentences(text):
        if _contains_forbidden(sentence):
            continue
        if _is_bare_section_heading(sentence):
            continue
        if _has_any_keyword(sentence, wanted):
            out.append(_clip_sentence(sentence, wanted))
        if len(out) >= max_sentences:
            break
    return out


def periodic_evidence_to_normalized_item(
    evidence: PeriodicReportEvidence,
    source_id: str = 'requests:company_announcements_pdf',
) -> NormalizedIntelItem:
    return NormalizedIntelItem(
        id=_normalized_id(source_id, evidence),
        source_id=source_id,
        title=evidence.title or _default_title(evidence.field, evidence.report_type),
        summary=evidence.summary[:260],
        layer=evidence.layer,
        direction='neutral',
        related_codes=[evidence.code] if evidence.code else [],
        related_names=[evidence.name] if evidence.name else [],
        related_sectors=[],
        evidence_type='periodic_report_section',
        published_at=evidence.published_at,
        fetched_at=_now(),
        url=evidence.source_url,
        source_url=evidence.source_url,
        trust_level='primary',
        confidence=int(evidence.confidence or 0),
        category=evidence.category,
        relevance_reason=evidence.raw_ref,
        matched_keywords=list(evidence.matched_keywords),
        time_windows=['swing', 'mid', 'long'],
        raw_ref=evidence.raw_ref,
    )


def _append_table_evidence(
    evidence: list[PeriodicReportEvidence],
    seen: set[tuple[str, str]],
    table: dict[str, Any],
    *,
    report_type: str,
    clean_title: str,
    source_url: str,
    published_at: str | None,
    code: str,
    name: str,
) -> None:
    table_matches = _classify_table_fields(table)
    if not table_matches:
        return
    section = str(table.get('section_guess') or 'table')
    table_text = _table_to_text(table)
    searchable = _searchable_text(table_text)
    for match in table_matches:
        field_name = str(match.get('field') or '')
        rule = _FIELD_RULE_BY_NAME.get(field_name)
        layer = str(match.get('layer') or (rule.get('layer') if rule else '') or field_name)
        category = str(match.get('category') or (rule.get('category') if rule else '') or field_name)
        if not field_name or not layer:
            continue
        evidence_text = _truncate_evidence(str(match.get('evidence_text') or table_text))
        key = (field_name, evidence_text)
        if key in seen:
            continue
        seen.add(key)
        if rule and not match.get('category'):
            category = _category_for_rule(rule, section, searchable)
        raw_ref = _raw_ref(
            field_name,
            report_type,
            section,
            evidence_text,
            source_url,
            page=int(table.get('page') or 0),
        )
        table_confidence = _confidence_from_table(table)
        matched_keywords = list(match.get('matched_keywords') or [])
        evidence.append(PeriodicReportEvidence(
            field=field_name,
            layer=layer,
            category=category,
            title=clean_title or _default_title(field_name, report_type),
            summary=evidence_text[:260],
            evidence_text=evidence_text,
            matched_keywords=matched_keywords,
            confidence=max(table_confidence, min(9, 5 + len(matched_keywords))),
            report_type=report_type,
            section=section,
            source_url=str(source_url or ''),
            published_at=published_at,
            raw_ref=raw_ref,
            code=_clean_code(code),
            name=_clean_text(name),
            page=int(table.get('page') or 0),
            table_header=list(table.get('header') or []),
            table_quality_score=float(match.get('table_quality_score') or _table_quality_score(table, field_name, matched_keywords)),
            table_values=dict(match.get('table_values') or {}),
            table_row_index=int(match.get('sort_index') or 0),
        ))


def _classify_table_fields(table: dict[str, Any]) -> list[dict[str, Any]]:
    if _is_query_table(table):
        return []
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    rows = table.get('rows') or []
    if not header_text or not rows:
        return []

    out: list[dict[str, Any]] = []
    out.extend(_customer_supplier_table_matches(table))
    out.extend(_business_segment_table_matches(table))
    out.extend(_inventory_table_matches(table))
    out.extend(_cost_table_matches(table))
    return _sort_table_matches([
        match for match in out
        if float(match.get('table_quality_score') or 0.0) >= _MIN_TABLE_QUALITY_SCORE
    ])


def _customer_supplier_table_matches(table: dict[str, Any]) -> list[dict[str, Any]]:
    header = [str(value) for value in table.get('header') or []]
    rows = table.get('rows') or []
    header_text = _clean_text(' '.join(header))
    row_text = _clean_text(' '.join(str(cell) for row in rows for cell in (row if isinstance(row, (list, tuple)) else [])))
    section = str(table.get('section_guess') or '').strip()
    combined = _clean_text(f'{section} {header_text} {row_text}')
    if section != 'customer_supplier' and not _has_any_keyword(combined, ['\u524d\u4e94\u5927\u5ba2\u6237', '\u524d\u4e94\u540d\u5ba2\u6237', '\u524d\u4e94\u5927\u4f9b\u5e94\u5546', '\u524d\u4e94\u540d\u4f9b\u5e94\u5546']):
        return []

    out: list[dict[str, Any]] = []
    customer_hits = _dedupe_keywords(
        _keyword_hits(combined, _TABLE_FIELD_CORE_TERMS['customer_concentration'])
    )
    supplier_hits = _dedupe_keywords(
        _keyword_hits(combined, _TABLE_FIELD_CORE_TERMS['supplier_concentration'])
    )
    has_customer = _has_any_keyword(combined, ['\u5ba2\u6237', '\u524d\u4e94\u540d\u5ba2\u6237', '\u524d\u4e94\u5927\u5ba2\u6237'])
    has_supplier = _has_any_keyword(combined, ['\u4f9b\u5e94\u5546', '\u524d\u4e94\u540d\u4f9b\u5e94\u5546', '\u524d\u4e94\u5927\u4f9b\u5e94\u5546'])
    has_sales = _has_any_keyword(combined, ['\u9500\u552e\u989d', '\u9500\u552e\u91d1\u989d', '\u9500\u552e\u603b\u989d'])
    has_purchase = _has_any_keyword(combined, ['\u91c7\u8d2d\u989d', '\u91c7\u8d2d\u91d1\u989d', '\u91c7\u8d2d\u603b\u989d'])
    evidence_text = _table_to_text(table)
    if has_customer and has_sales:
        out.append(_table_match(
            table,
            field='customer_concentration',
            layer='customer_supplier',
            category='customer_concentration',
            matched_keywords=customer_hits,
            evidence_text=evidence_text,
            table_values=_concentration_values(header, rows, amount_terms=('\u9500\u552e\u989d', '\u9500\u552e\u91d1\u989d'), share_terms=('\u5360\u6bd4', '\u6bd4\u4f8b')),
        ))
    if has_supplier and has_purchase:
        out.append(_table_match(
            table,
            field='supplier_concentration',
            layer='customer_supplier',
            category='supplier_concentration',
            matched_keywords=supplier_hits,
            evidence_text=evidence_text,
            table_values=_concentration_values(header, rows, amount_terms=('\u91c7\u8d2d\u989d', '\u91c7\u8d2d\u91d1\u989d'), share_terms=('\u5360\u6bd4', '\u6bd4\u4f8b')),
        ))
    return out


def _business_segment_table_matches(table: dict[str, Any]) -> list[dict[str, Any]]:
    header = [str(value) for value in table.get('header') or []]
    rows = [row for row in (table.get('rows') or []) if isinstance(row, (list, tuple))]
    if not _is_business_segment_table(table):
        return []

    out: list[dict[str, Any]] = []
    segment_type = _segment_type(header)
    segment_index = _segment_name_index(header)
    header_hits = _keyword_hits(_clean_text(' '.join(header)), _TABLE_FIELD_CORE_TERMS['business_segments'])
    for row_index, row in enumerate(rows[:12]):
        values = _segment_values(header, row, segment_index=segment_index, segment_type=segment_type)
        segment_name = str(values.get('segment_name') or '').strip()
        if not segment_name or _is_total_row(segment_name) or _is_segment_header_row(segment_name):
            continue
        row_text = _row_evidence_text(header, row)
        matched = _dedupe_keywords(header_hits + _keyword_hits(row_text, _BUSINESS_SEGMENT_HEADER_TERMS))
        out.append(_table_match(
            table,
            field='business_segments',
            layer='financial',
            category='business_segments',
            matched_keywords=matched,
            evidence_text=row_text,
            table_values=values,
            sort_index=row_index,
        ))
        if _is_export_segment_row(segment_name, row_text, segment_type):
            export_hits = _dedupe_keywords(_keyword_hits(row_text, _TABLE_FIELD_CORE_TERMS['export']) + _keyword_hits(row_text, _EXPORT_ROW_TERMS))
            out.append(_table_match(
                table,
                field='export',
                layer='export',
                category='region_revenue',
                matched_keywords=export_hits,
                evidence_text=row_text,
                table_values=values,
                sort_index=row_index,
            ))
    return out


def _inventory_table_matches(table: dict[str, Any]) -> list[dict[str, Any]]:
    header = [str(value) for value in table.get('header') or []]
    rows = [row for row in (table.get('rows') or []) if isinstance(row, (list, tuple))]
    if not _is_inventory_table(table):
        return []

    out: list[dict[str, Any]] = []
    item_index = _first_header_index(header, ('\u9879\u76ee', '\u5b58\u8d27\u79cd\u7c7b', '\u7c7b\u522b'), default=0)
    header_hits = _keyword_hits(_clean_text(' '.join(header)), _TABLE_FIELD_CORE_TERMS['inventory'])
    for row_index, row in enumerate(rows[:12]):
        row_text = _row_evidence_text(header, row)
        item_name = _cell(row, item_index)
        row_hits = _keyword_hits(row_text, _INVENTORY_COMPONENT_TERMS)
        if not row_hits and item_name != '\u5b58\u8d27':
            continue
        if not _has_amount_ratio_yoy_signal(row_text):
            continue
        values = {
            'item_name': item_name or _cell(row, 0),
            'book_balance': _header_value(header, row, include=('\u8d26\u9762\u4f59\u989d', '\u8d26\u9762\u4f59\u989d', '\u671f\u672b\u4f59\u989d', '\u4f59\u989d'), exclude=('\u8d26\u9762\u4ef7\u503c',)),
            'write_down': _header_value(header, row, include=('\u8dcc\u4ef7\u51c6\u5907',)),
            'book_value': _header_value(header, row, include=('\u8d26\u9762\u4ef7\u503c',)),
        }
        out.append(_table_match(
            table,
            field='inventory',
            layer='inventory',
            category='inventory_ar_margin',
            matched_keywords=_dedupe_keywords(header_hits + row_hits),
            evidence_text=row_text,
            table_values={key: value for key, value in values.items() if value},
            sort_index=row_index,
        ))
    return out


def _cost_table_matches(table: dict[str, Any]) -> list[dict[str, Any]]:
    header = [str(value) for value in table.get('header') or []]
    rows = [row for row in (table.get('rows') or []) if isinstance(row, (list, tuple))]
    if not _is_cost_table(table):
        return []

    out: list[dict[str, Any]] = []
    item_index = _first_header_index(header, ('\u6210\u672c\u9879\u76ee', '\u9879\u76ee', '\u6210\u672c\u6784\u6210'), default=0)
    header_hits = _keyword_hits(_clean_text(' '.join(header)), _TABLE_FIELD_CORE_TERMS['cost'])
    for row_index, row in enumerate(rows[:12]):
        row_text = _row_evidence_text(header, row)
        row_cell_text = _clean_text(' '.join(str(value) for value in row))
        row_hits = _keyword_hits(row_cell_text, _COST_COMPONENT_TERMS)
        if not row_hits:
            continue
        values = {
            'item_name': _cell(row, item_index) or _cell(row, 0),
            'cost': _header_value(header, row, include=('\u8425\u4e1a\u6210\u672c', '\u6210\u672c', '\u91d1\u989d'), exclude=('\u5360\u6bd4', '\u6bd4\u4f8b')),
            'share': _header_value(header, row, include=('\u5360\u6bd4', '\u6bd4\u4f8b')),
        }
        out.append(_table_match(
            table,
            field='cost',
            layer='cost',
            category='cost_structure',
            matched_keywords=_dedupe_keywords(header_hits + row_hits),
            evidence_text=row_text,
            table_values={key: value for key, value in values.items() if value},
            sort_index=row_index,
        ))
    return out


def _table_match(
    table: dict[str, Any],
    *,
    field: str,
    layer: str,
    category: str,
    matched_keywords: list[str],
    evidence_text: str,
    table_values: dict[str, Any] | None = None,
    sort_index: int = 0,
) -> dict[str, Any]:
    matched = _dedupe_keywords(matched_keywords)
    return {
        'field': field,
        'layer': layer,
        'category': category,
        'matched_keywords': matched,
        'evidence_text': evidence_text,
        'table_values': dict(table_values or {}),
        'table_quality_score': _table_quality_score(table, field, matched),
        'sort_index': int(sort_index or 0),
    }


def _is_business_segment_table(table: dict[str, Any]) -> bool:
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    section = str(table.get('section_guess') or '').strip()
    if section == 'business_segments':
        return bool(_has_any_keyword(header_text, _BUSINESS_SEGMENT_HEADER_TERMS))
    return bool(
        _has_any_keyword(header_text, ('\u5206\u884c\u4e1a', '\u5206\u4ea7\u54c1', '\u5206\u5730\u533a'))
        and _has_any_keyword(header_text, ('\u8425\u4e1a\u6536\u5165', '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165', '\u6bdb\u5229\u7387'))
    )


def _is_inventory_table(table: dict[str, Any]) -> bool:
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    row_text = _clean_text(' '.join(str(cell) for row in table.get('rows') or [] for cell in (row if isinstance(row, (list, tuple)) else [])))
    section = str(table.get('section_guess') or '').strip()
    if _has_any_keyword(header_text, ('\u8d26\u9f84', '\u91d1\u878d\u8d44\u4ea7', '\u6743\u76ca\u5de5\u5177', '\u80a1\u4e1c\u6743\u76ca')):
        return False
    has_inventory_header = _has_any_keyword(header_text, ('\u5b58\u8d27', '\u8dcc\u4ef7\u51c6\u5907', '\u8d26\u9762\u4ef7\u503c', '\u8d26\u9762\u4f59\u989d'))
    has_component_row = _has_any_keyword(row_text, ('\u539f\u6750\u6599', '\u5e93\u5b58\u5546\u54c1', '\u53d1\u51fa\u5546\u54c1'))
    if section == 'inventory':
        return bool(has_inventory_header or has_component_row)
    return bool('\u5b58\u8d27' in header_text and (has_component_row or '\u8dcc\u4ef7\u51c6\u5907' in header_text or '\u8d26\u9762\u4ef7\u503c' in header_text))


def _is_cost_table(table: dict[str, Any]) -> bool:
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    section = str(table.get('section_guess') or '').strip()
    if section == 'business_segments':
        return False
    strong_header = _has_any_keyword(header_text, ('\u6210\u672c\u6784\u6210', '\u6210\u672c\u9879\u76ee', '\u4e3b\u8425\u4e1a\u52a1\u6210\u672c'))
    if section == 'cost':
        return bool(strong_header or '\u8425\u4e1a\u6210\u672c' in header_text)
    return bool(strong_header)


def _segment_type(header: list[str]) -> str:
    header_text = _clean_text(' '.join(header))
    if '\u5206\u5730\u533a' in header_text or '\u5730\u533a' in header_text:
        return 'region'
    if '\u5206\u4ea7\u54c1' in header_text or '\u4ea7\u54c1' in header_text:
        return 'product'
    if '\u5206\u884c\u4e1a' in header_text or '\u884c\u4e1a' in header_text:
        return 'industry'
    return 'segment'


def _segment_name_index(header: list[str]) -> int:
    return _first_header_index(header, ('\u5206\u884c\u4e1a', '\u5206\u4ea7\u54c1', '\u5206\u5730\u533a', '\u884c\u4e1a', '\u4ea7\u54c1', '\u5730\u533a', '\u9879\u76ee'), default=0)


def _segment_values(
    header: list[str],
    row: list[str] | tuple[str, ...],
    *,
    segment_index: int,
    segment_type: str,
) -> dict[str, Any]:
    values = {
        'segment_name': _cell(row, segment_index),
        'segment_type': segment_type,
        'revenue': _header_value(header, row, include=('\u8425\u4e1a\u6536\u5165', '\u4e3b\u8425\u4e1a\u52a1\u6536\u5165', '\u6536\u5165'), exclude=('\u589e\u51cf', '\u540c\u6bd4', '\u4e0a\u5e74', '\u53d8\u52a8')),
        'cost': _header_value(header, row, include=('\u8425\u4e1a\u6210\u672c', '\u6210\u672c'), exclude=('\u589e\u51cf', '\u540c\u6bd4', '\u4e0a\u5e74', '\u53d8\u52a8')),
        'gross_margin': _header_value(header, row, include=('\u6bdb\u5229\u7387',), exclude=('\u589e\u51cf', '\u540c\u6bd4', '\u4e0a\u5e74', '\u53d8\u52a8')),
        'revenue_yoy': _header_value(header, row, include=('\u8425\u4e1a\u6536\u5165', '\u6536\u5165'), require=('\u589e\u51cf', '\u540c\u6bd4', '\u4e0a\u5e74', '\u53d8\u52a8')),
        'cost_yoy': _header_value(header, row, include=('\u8425\u4e1a\u6210\u672c', '\u6210\u672c'), require=('\u589e\u51cf', '\u540c\u6bd4', '\u4e0a\u5e74', '\u53d8\u52a8')),
        'gross_margin_change': _header_value(header, row, include=('\u6bdb\u5229\u7387',), require=('\u589e\u51cf', '\u540c\u6bd4', '\u4e0a\u5e74', '\u53d8\u52a8')),
    }
    return {key: value for key, value in values.items() if value}


def _concentration_values(
    header: list[str],
    rows: list[Any],
    *,
    amount_terms: tuple[str, ...],
    share_terms: tuple[str, ...],
) -> dict[str, Any]:
    first = next((row for row in rows if isinstance(row, (list, tuple))), [])
    values = {
        'amount': _header_value(header, first, include=amount_terms),
        'share': _header_value(header, first, include=share_terms),
    }
    return {key: value for key, value in values.items() if value}


def _header_value(
    header: list[str],
    row: list[str] | tuple[str, ...] | Any,
    *,
    include: tuple[str, ...],
    exclude: tuple[str, ...] = (),
    require: tuple[str, ...] = (),
) -> str:
    if not isinstance(row, (list, tuple)):
        return ''
    for index, title in enumerate(header):
        text = str(title or '')
        if not any(term in text for term in include):
            continue
        if exclude and any(term in text for term in exclude):
            continue
        if require and not any(term in text for term in require):
            continue
        value = _cell(row, index)
        if value:
            return value
    return ''


def _first_header_index(header: list[str], terms: tuple[str, ...], *, default: int) -> int:
    for index, title in enumerate(header):
        if any(term in str(title or '') for term in terms):
            return index
    return default


def _cell(row: list[str] | tuple[str, ...] | Any, index: int) -> str:
    if not isinstance(row, (list, tuple)):
        return ''
    if index < 0 or index >= len(row):
        return ''
    return _clean_text(row[index])


def _row_evidence_text(header: list[str], row: list[str] | tuple[str, ...]) -> str:
    pairs: list[str] = []
    for index, value in enumerate(row):
        cell = _clean_text(value)
        if not cell:
            continue
        title = _clean_text(header[index]) if index < len(header) else ''
        pairs.append(f'{title}: {cell}' if title else cell)
    return _clean_text(' | '.join(pairs))


def _is_export_segment_row(segment_name: str, row_text: str, segment_type: str) -> bool:
    text = _clean_text(f'{segment_name} {row_text}')
    if not _has_any_keyword(text, _EXPORT_ROW_TERMS):
        return False
    return segment_type == 'region' or _has_any_keyword(text, ('\u5916\u9500', '\u51fa\u53e3', '\u5883\u5916', '\u6d77\u5916', '\u56fd\u5916'))


def _is_total_row(value: str) -> bool:
    text = _clean_text(value)
    return (
        text in {'\u5408\u8ba1', '\u5c0f\u8ba1', '\u603b\u8ba1', '\u5176\u4e2d', '\u5176\u4e2d\uff1a', '\u5176\u4e2d:', 'total'}
        or text.endswith('\u5408\u8ba1')
    )


def _is_segment_header_row(value: str) -> bool:
    text = _clean_text(value)
    return text in set(_SEGMENT_HEADER_ROW_TERMS)


def _table_quality_score(table: dict[str, Any], field_name: str = '', matched_keywords: list[str] | None = None) -> float:
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    rows = table.get('rows') or []
    row_text = _clean_text(' '.join(str(cell) for row in rows for cell in (row if isinstance(row, (list, tuple)) else [])))
    section = str(table.get('section_guess') or '').strip()
    score = 0.0
    if section and section != 'table':
        score += 0.2
    header_hits = _table_header_hits(header_text, field_name)
    if header_hits:
        score += 0.3
    if len(rows) >= 2:
        score += 0.1
    if _has_amount_ratio_yoy_signal(f'{header_text} {row_text}'):
        score += 0.2
    if _is_generic_financial_table(section, header_text, header_hits):
        score -= 0.3
    return round(max(0.0, min(1.0, score)), 3)


def _table_header_hits(header_text: str, field_name: str = '') -> list[str]:
    if field_name:
        return _keyword_hits(header_text, tuple(_TABLE_FIELD_CORE_TERMS.get(field_name, ())))
    hits: list[str] = []
    for terms in _TABLE_FIELD_CORE_TERMS.values():
        hits.extend(_keyword_hits(header_text, tuple(terms)))
    return _dedupe_keywords(hits)


def _has_amount_ratio_yoy_signal(text: str) -> bool:
    value = str(text or '')
    if any(term in value for term in ('%', '\uff05', '\u4ebf', '\u4e07', '\u5143', '\u540c\u6bd4', '\u589e\u51cf', '\u6bdb\u5229\u7387', '\u5360\u6bd4')):
        return any(ch.isdigit() for ch in value)
    return bool(re.search(r'\d[\d,]*(?:\.\d+)?', value))


def _is_generic_financial_table(section: str, header_text: str, header_hits: list[str]) -> bool:
    if header_hits:
        return False
    if section == 'financial_statements':
        return True
    return _has_any_keyword(header_text, _GENERIC_FINANCIAL_HEADER_TERMS)


def _sort_table_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        matches,
        key=lambda match: (
            -float(match.get('table_quality_score') or 0.0),
            _table_field_priority(str(match.get('field') or '')),
            int(match.get('sort_index') or 0),
            str(match.get('evidence_text') or ''),
        ),
    )


def _sort_periodic_evidence(evidence: list[PeriodicReportEvidence]) -> list[PeriodicReportEvidence]:
    return sorted(
        evidence,
        key=lambda item: (
            -float(item.table_quality_score or 0.0),
            _table_field_priority(str(item.field or '')),
            int(item.table_row_index or 0),
            str(item.raw_ref or ''),
        ),
    )


def _table_field_priority(field_name: str) -> int:
    order = {
        'business_segments': 0,
        'export': 1,
        'cost': 2,
        'inventory': 3,
        'customer_concentration': 4,
        'supplier_concentration': 5,
        'customer_supplier': 6,
        'financial': 7,
    }
    return order.get(field_name, 20)


def _table_missing_reason_for_field(field_name: str, tables: list[dict[str, Any]]) -> str:
    candidates = [
        table for table in tables
        if _table_matches_expected_field(field_name, table)
    ]
    if not candidates:
        return ''
    if all(not (table.get('rows') or []) for table in candidates):
        return 'table_rows_empty'
    if not any(
        _table_match_satisfies_field(field_name, str(match.get('field') or ''))
        for table in candidates
        for match in _classify_table_fields(table)
    ):
        return 'table_present_but_header_unmatched'
    return ''


def _table_match_satisfies_field(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    if expected == 'customer_supplier' and actual in {'customer_concentration', 'supplier_concentration'}:
        return True
    return False


def _table_matches_expected_field(field_name: str, table: dict[str, Any]) -> bool:
    section = str(table.get('section_guess') or '').strip()
    if section and section in _TABLE_SECTION_BY_FIELD.get(field_name, set()):
        return True
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    row_text = _clean_text(' '.join(
        str(cell)
        for row in table.get('rows') or []
        for cell in (row if isinstance(row, (list, tuple)) else [])
    ))
    for rule in _TABLE_CLASSIFIER_RULES:
        if str(rule.get('field') or '') != field_name:
            continue
        return bool(
            _keyword_hits(header_text, tuple(rule.get('header_keywords') or ()))
            or _keyword_hits(row_text, tuple(rule.get('row_keywords') or ()))
        )
    return False


def _is_query_table(table: dict[str, Any]) -> bool:
    header_text = _clean_text(' '.join(str(value) for value in table.get('header') or []))
    return _has_any_keyword(header_text, list(_TABLE_QUERY_HEADER_TERMS))


def _normalise_table_block(table: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(table, dict):
        return {}
    header = [_clean_text(value) for value in table.get('header') or [] if _clean_text(value)]
    rows = [
        [_clean_text(value) for value in row if _clean_text(value)]
        for row in (table.get('rows') or [])
        if isinstance(row, (list, tuple)) and any(_clean_text(value) for value in row)
    ]
    return {
        'page': int(table.get('page') or 0),
        'bbox': list(table.get('bbox') or []),
        'section_guess': _clean_text(table.get('section_guess') or _guess_table_section(header, rows)),
        'header': header,
        'rows': rows,
        'confidence': table.get('confidence') or 0,
        'extractor': _clean_text(table.get('extractor') or ''),
    }


def _guess_table_section(header: list[str], rows: list[list[str]]) -> str:
    text = _table_text(header, rows)
    if _has_any_keyword(text, ['\u524d\u4e94\u5927\u5ba2\u6237', '\u5ba2\u6237\u540d\u79f0', '\u4f9b\u5e94\u5546']):
        return 'customer_supplier'
    if _has_any_keyword(text, ['\u5b58\u8d27', '\u8dcc\u4ef7\u51c6\u5907', '\u5e93\u5b58']):
        return 'inventory'
    if _has_any_keyword(text, ['\u6210\u672c\u9879\u76ee', '\u8425\u4e1a\u6210\u672c', '\u539f\u6750\u6599']):
        return 'cost'
    if _has_any_keyword(text, ['\u5206\u5730\u533a', '\u5883\u5916', '\u6d77\u5916', '\u5206\u884c\u4e1a', '\u5206\u4ea7\u54c1']):
        return 'business_segments'
    return 'table'


def _table_to_text(table: dict[str, Any]) -> str:
    header = [str(value) for value in table.get('header') or [] if str(value)]
    rows = [
        [str(value) for value in row if str(value)]
        for row in (table.get('rows') or [])[:4]
        if isinstance(row, (list, tuple))
    ]
    lines = []
    if header:
        lines.append(' | '.join(header))
    lines.extend(' | '.join(row) for row in rows if row)
    return _clean_text('\n'.join(lines))


def _table_text(header: list[str], rows: list[list[str]]) -> str:
    return ' '.join(header + [cell for row in rows for cell in row])


def _tables_searchable_text(tables: list[dict[str, Any]]) -> str:
    return ' '.join(_table_to_text(table) for table in tables)


def _confidence_from_table(table: dict[str, Any]) -> int:
    try:
        value = float(table.get('confidence') or 0)
    except Exception:
        value = 0
    if value <= 0:
        return 7
    if value <= 1:
        return max(1, min(10, int(round(value * 10))))
    return max(1, min(10, int(round(value))))


def _section_texts_from_index(sections: list[dict[str, Any]] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get('section_id') or '').strip()
        section_text = str(section.get('text') or '').strip()
        if section_id and section_text:
            out[section_id] = section_text
    return out


def _section_keywords_for_heading(line: str, section_id: str) -> list[str]:
    for candidate_id, keywords in _SECTION_RULES:
        if candidate_id != section_id:
            continue
        return [keyword for keyword in keywords if keyword in line]
    return []


def _section_for_heading(line: str) -> str:
    for section, keywords in _SECTION_RULES:
        if _has_any_keyword(line, keywords):
            return section
    return ''


def _is_bare_section_heading(line: str) -> bool:
    text = _clean_text(line)
    if not text or not _section_for_heading(text):
        return False
    content_markers = ('\u3002', '\uff0c', '\uff1b', ';', ':', '\uff1a', '%', '\uff05', '\u5143',
                       '\u5360', '\u4e3a', '\u589e\u957f', '\u63d0\u5347', '\u589e\u52a0',
                       '\u4e0b\u964d', '\u540c\u6bd4')
    if any(ch.isdigit() for ch in text) or any(marker in text for marker in content_markers):
        return False
    return bool(len(text) <= 40 and _looks_like_heading(text))


def _looks_like_heading(line: str) -> bool:
    text = _clean_text(line)
    if any(marker in text for marker in ('\u3002', '\uff0c', '\uff1b', '%', '\uff05', '\u5143')):
        return False
    if len(text) <= 36:
        return True
    return bool(re.match(r'^[\d\uff08\uff09\(\)\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u3001 .-]+$', text[:8]))


def _category_for_rule(rule: dict[str, Any], section: str, searchable: str) -> str:
    field_name = str(rule['field'])
    if field_name in {'financial', 'demand'} and (
        section == 'main_business'
        or '\u4e3b\u8425\u4e1a\u52a1\u6784\u6210' in searchable
        or '\u4e3b\u8425\u4ea7\u54c1' in searchable
    ):
        return 'main_business_composition'
    return str(rule['category'])


def _searchable_text(text: str) -> str:
    return ' '.join(sentence for sentence in _sentences(text) if not _contains_forbidden(sentence))


def _sentences(text: str) -> list[str]:
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    parts = [
        _clean_text(part)
        for part in _SENTENCE_SPLIT_RE.split(cleaned)
        if _clean_text(part)
    ]
    return parts or [cleaned]


def _clip_sentence(sentence: str, keywords: list[str]) -> str:
    cleaned = _clean_text(sentence)
    if len(cleaned) <= 220:
        return cleaned
    positions = [cleaned.find(keyword) for keyword in keywords if keyword in cleaned]
    anchor = min((pos for pos in positions if pos >= 0), default=0)
    start = max(0, anchor - 80)
    end = min(len(cleaned), anchor + 140)
    return cleaned[start:end].strip()


def _truncate_evidence(text: str) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= _MAX_EVIDENCE_CHARS:
        return cleaned
    return cleaned[:_MAX_EVIDENCE_CHARS].rstrip()


def _contains_forbidden(text: str) -> bool:
    upper = str(text or '').upper()
    return any(term in text or term.upper() in upper for term in _FORBIDDEN_TERMS)


def _keyword_hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for keyword in keywords:
        if not keyword:
            continue
        if keyword == '\u4eba\u5de5':
            if re.search(r'\u4eba\u5de5(?!\u667a\u80fd)', text):
                out.append(keyword)
            continue
        if keyword in text:
            out.append(keyword)
    return out


def _dedupe_keywords(keywords: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        cleaned = str(keyword or '').strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _has_any_keyword(text: str, keywords: tuple[str, ...] | list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(' ', str(value or '')).strip()


def _clean_code(value: str) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _default_title(field_name: str, report_type: str) -> str:
    label = report_type if report_type and report_type != 'unknown' else 'periodic'
    return f'{label} report {field_name} evidence'


def _raw_ref(
    field_name: str,
    report_type: str,
    section: str,
    evidence_text: str,
    source_url: str,
    *,
    page: int = 0,
) -> str:
    page_ref = f'page{int(page or 0)}' if int(page or 0) > 0 else 'text'
    digest = hashlib.sha256(
        f'{field_name}|{report_type}|{section}|{page_ref}|{evidence_text}|{source_url}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'periodic_report:{report_type}:{section}:{page_ref}:{field_name}:{digest}'


def _normalized_id(source_id: str, evidence: PeriodicReportEvidence) -> str:
    digest = hashlib.sha256(
        f'{source_id}|{evidence.raw_ref}|{evidence.evidence_text}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'periodic:{source_id}:{evidence.field}:{digest}'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


__all__ = [
    'PeriodicReportEvidence',
    'build_periodic_report_section_index',
    'detect_report_type',
    'extract_periodic_report_sentences',
    'parse_periodic_report_text',
    'periodic_report_field_missing_reasons',
    'periodic_report_table_status',
    'periodic_evidence_to_normalized_item',
    'split_report_sections',
]
