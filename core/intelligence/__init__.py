"""Intelligence source contracts for EvidenceGraph collection."""

from core.intelligence.collector_base import Collector, CollectorResult
from core.intelligence.evidence_adapter import evidence_items_to_nodes_edges
from core.intelligence.models import NormalizedIntelItem, RawIntelItem, SourceSpec

__all__ = [
    'SourceSpec',
    'RawIntelItem',
    'NormalizedIntelItem',
    'Collector',
    'CollectorResult',
    'evidence_items_to_nodes_edges',
]
