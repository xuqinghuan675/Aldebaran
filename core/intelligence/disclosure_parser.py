"""Announcement disclosure text parser for intelligence evidence."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.intelligence.models import NormalizedIntelItem


_SENTENCE_SPLIT_RE = re.compile(r'(?<=[\u3002\uff01\uff1f!?;；])\s*|\n+')
_SPACE_RE = re.compile(r'\s+')
_MAX_EVIDENCE_CHARS = 420


@dataclass(frozen=True)
class DisclosureEvidence:
    field: str
    layer: str
    category: str
    title: str
    summary: str
    evidence_text: str
    source_url: str
    published_at: str | None
    matched_keywords: list[str] = field(default_factory=list)
    confidence: int = 0
    parser_status: str = 'ok'
    raw_ref: str = ''
    code: str = ''
    name: str = ''


_FIELD_RULES: tuple[dict[str, Any], ...] = (
    {
        'field': 'order_contract',
        'layer': 'order_contract',
        'category': 'announcement_order_contract',
        'keywords': (
            '\u4e2d\u6807',
            '\u5408\u540c',
            '\u8ba2\u5355',
            '\u6846\u67b6\u534f\u8bae',
            '\u91c7\u8d2d\u534f\u8bae',
            '\u9500\u552e\u5408\u540c',
        ),
    },
    {
        'field': 'capacity',
        'layer': 'capacity',
        'category': 'announcement_capacity',
        'keywords': (
            '\u6269\u4ea7',
            '\u6295\u4ea7',
            '\u4ea7\u7ebf',
            '\u4ea7\u80fd',
            '\u52df\u6295\u9879\u76ee',
            '\u5efa\u8bbe\u9879\u76ee',
            '\u8bd5\u751f\u4ea7',
        ),
    },
    {
        'field': 'risk',
        'layer': 'risk',
        'category': 'announcement_risk',
        'keywords': (
            '\u8bc9\u8bbc',
            '\u4ef2\u88c1',
            '\u5904\u7f5a',
            '\u95ee\u8be2',
            '\u76d1\u7ba1\u51fd',
            '\u51cf\u6301',
            '\u89e3\u7981',
            '\u8d28\u62bc',
            '\u9000\u5e02\u98ce\u9669',
        ),
    },
    {
        'field': 'financial',
        'layer': 'financial',
        'category': 'announcement_financial',
        'keywords': (
            '\u4e1a\u7ee9\u9884\u544a',
            '\u4e1a\u7ee9\u5feb\u62a5',
            '\u8425\u6536',
            '\u8425\u4e1a\u6536\u5165',
            '\u51c0\u5229\u6da6',
            '\u6bdb\u5229\u7387',
            '\u73b0\u91d1\u6d41',
        ),
    },
    {
        'field': 'customer_supplier',
        'layer': 'customer_supplier',
        'category': 'announcement_customer_supplier',
        'keywords': (
            '\u5ba2\u6237',
            '\u4f9b\u5e94\u5546',
            '\u91c7\u8d2d',
            '\u9500\u552e',
            '\u4f9b\u5e94\u94fe',
            '\u5927\u5ba2\u6237',
        ),
    },
    {
        'field': 'inventory',
        'layer': 'inventory',
        'category': 'announcement_inventory',
        'keywords': (
            '\u5e93\u5b58',
            '\u5b58\u8d27',
            '\u5907\u8d27',
            '\u53bb\u5e93',
            '\u8865\u5e93',
        ),
    },
    {
        'field': 'export',
        'layer': 'export',
        'category': 'announcement_export',
        'keywords': (
            '\u6d77\u5916',
            '\u51fa\u53e3',
            '\u5916\u8d38',
            '\u5173\u7a0e',
            '\u5883\u5916',
            '\u56fd\u9645\u5ba2\u6237',
        ),
    },
)


_FORBIDDEN_TERMS = (
    '\u004b\u7ebf',
    '\u5747\u7ebf',
    ''.join(('M', 'A', 'C', 'D')),
    ''.join(('R', 'S', 'I')),
    ''.join(('K', 'D', 'J')),
    '\u652f\u6491',
    '\u538b\u529b',
    '\u4e70\u70b9',
    '\u6b62\u635f',
    '\u89e6\u53d1\u4ef7',
    '\u5931\u6548\u4ef7',
)


def parse_announcement_text(
    text: str,
    *,
    title: str = '',
    source_url: str = '',
    published_at: str | None = None,
    code: str = '',
    name: str = '',
) -> list[DisclosureEvidence]:
    body = _clean_text(text)
    clean_title = _clean_text(title)
    if not body:
        return []

    evidence: list[DisclosureEvidence] = []
    for classification in classify_disclosure_evidence(body, title=clean_title):
        keywords = list(classification['matched_keywords'])
        snippets = extract_relevant_sentences(body, keywords, max_sentences=3)
        if not snippets and _has_any_keyword(clean_title, keywords) and not _contains_forbidden(clean_title):
            snippets = [clean_title]
        if not snippets:
            continue
        evidence_text = _truncate_evidence('\n'.join(snippets))
        field_name = str(classification['field'])
        raw_ref = _raw_ref(field_name, clean_title, evidence_text, source_url)
        evidence.append(DisclosureEvidence(
            field=field_name,
            layer=str(classification['layer']),
            category=str(classification['category']),
            title=clean_title or _default_title(field_name),
            summary=evidence_text[:260],
            evidence_text=evidence_text,
            source_url=str(source_url or ''),
            published_at=published_at,
            matched_keywords=keywords,
            confidence=int(classification['confidence']),
            parser_status='ok',
            raw_ref=raw_ref,
            code=_clean_code(code),
            name=_clean_text(name),
        ))
    return evidence


def parse_announcement_pdf_text(
    text: str,
    *,
    title: str = '',
    source_url: str = '',
    published_at: str | None = None,
    code: str = '',
    name: str = '',
) -> list[DisclosureEvidence]:
    return parse_announcement_text(
        text,
        title=title,
        source_url=source_url,
        published_at=published_at,
        code=code,
        name=name,
    )


def classify_disclosure_evidence(text: str, title: str = '') -> list[dict[str, Any]]:
    searchable = ' '.join([
        sentence
        for sentence in _sentences(f'{title}\u3002{text}')
        if not _contains_forbidden(sentence)
    ])
    if not searchable:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in _FIELD_RULES:
        matched = [
            keyword
            for keyword in rule['keywords']
            if keyword and keyword in searchable
        ]
        field_name = str(rule['field'])
        if field_name == 'customer_supplier':
            matched = _customer_supplier_keywords(matched, searchable)
        if not matched or field_name in seen:
            continue
        seen.add(field_name)
        out.append({
            'field': field_name,
            'layer': rule['layer'],
            'category': rule['category'],
            'matched_keywords': matched,
            'confidence': min(8, 5 + len(matched)),
        })
    return out


def _customer_supplier_keywords(matched: list[str], searchable: str) -> list[str]:
    strong_terms = {
        '\u5ba2\u6237',
        '\u4f9b\u5e94\u5546',
        '\u4f9b\u5e94\u94fe',
        '\u5927\u5ba2\u6237',
    }
    if strong_terms & set(matched):
        return matched

    contextual_terms = (
        '\u91c7\u8d2d\u534f\u8bae',
        '\u91c7\u8d2d\u5408\u540c',
        '\u91c7\u8d2d\u8ba2\u5355',
        '\u9500\u552e\u5408\u540c',
        '\u9500\u552e\u8ba2\u5355',
    )
    if any(term in searchable for term in contextual_terms):
        return [
            keyword for keyword in matched
            if keyword in {'\u91c7\u8d2d', '\u9500\u552e'}
        ]
    return []


def extract_relevant_sentences(
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
        if _has_any_keyword(sentence, wanted):
            out.append(_clip_sentence(sentence, wanted))
        if len(out) >= max_sentences:
            break
    return out


def disclosure_evidence_to_normalized_item(
    evidence: DisclosureEvidence,
    source_id: str = 'requests:company_announcements_pdf',
) -> NormalizedIntelItem:
    return NormalizedIntelItem(
        id=_normalized_id(source_id, evidence),
        source_id=source_id,
        title=evidence.title or _default_title(evidence.field),
        summary=evidence.summary[:260],
        layer=evidence.layer,
        direction='neutral',
        related_codes=[evidence.code] if evidence.code else [],
        related_names=[evidence.name] if evidence.name else [],
        related_sectors=[],
        evidence_type='announcement_pdf',
        published_at=evidence.published_at,
        fetched_at=_now(),
        url=evidence.source_url,
        source_url=evidence.source_url,
        trust_level='primary',
        confidence=int(evidence.confidence or 0),
        category=evidence.category,
        relevance_reason=evidence.raw_ref,
        matched_keywords=list(evidence.matched_keywords),
        time_windows=['short', 'swing', 'mid'],
        raw_ref=evidence.raw_ref,
    )


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
    if len(cleaned) <= 180:
        return cleaned
    positions = [cleaned.find(keyword) for keyword in keywords if keyword in cleaned]
    anchor = min((pos for pos in positions if pos >= 0), default=0)
    start = max(0, anchor - 70)
    end = min(len(cleaned), anchor + 110)
    return cleaned[start:end].strip()


def _truncate_evidence(text: str) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= _MAX_EVIDENCE_CHARS:
        return cleaned
    return cleaned[:_MAX_EVIDENCE_CHARS].rstrip()


def _has_any_keyword(text: str, keywords: list[str] | tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _contains_forbidden(text: str) -> bool:
    upper = str(text or '').upper()
    return any(term in text or term.upper() in upper for term in _FORBIDDEN_TERMS)


def _clean_text(value: str) -> str:
    return _SPACE_RE.sub(' ', str(value or '')).strip()


def _clean_code(value: str) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _default_title(field_name: str) -> str:
    return f'{field_name} disclosure evidence'


def _raw_ref(field_name: str, title: str, evidence_text: str, source_url: str) -> str:
    digest = hashlib.sha256(
        f'{field_name}|{title}|{evidence_text}|{source_url}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'disclosure:{field_name}:{digest}'


def _normalized_id(source_id: str, evidence: DisclosureEvidence) -> str:
    digest = hashlib.sha256(
        f'{source_id}|{evidence.raw_ref}|{evidence.evidence_text}'.encode('utf-8')
    ).hexdigest()[:16]
    return f'disclosure:{source_id}:{evidence.field}:{digest}'


def _now() -> str:
    return datetime.now().isoformat(timespec='seconds')


__all__ = [
    'DisclosureEvidence',
    'classify_disclosure_evidence',
    'disclosure_evidence_to_normalized_item',
    'extract_relevant_sentences',
    'parse_announcement_pdf_text',
    'parse_announcement_text',
]
