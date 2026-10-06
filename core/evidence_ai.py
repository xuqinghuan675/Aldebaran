"""EvidencePack-only intelligence analysis.

This module intentionally performs a conservative local analysis for the
intelligence page. It does not add facts beyond the pack and never emits
trade-entry fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.evidence_graph import EvidencePack


@dataclass
class LayerVerdict:
    direction: str
    confidence: int
    summary: str
    dominant_variables: list[str] = field(default_factory=list)
    expectation_gap: str = 'unknown'
    time_window: str = 'mixed'
    drag_reason: str = ''


@dataclass
class ForecastHypothesis:
    title: str
    direction: str
    time_window: str
    confidence: int
    based_on: list[str]
    missing_or_risk: list[str]
    summary: str


@dataclass
class EvidenceAnalysis:
    overall_direction: str
    confidence: int
    intel_stage: str
    layer_verdicts: dict[str, LayerVerdict]
    time_window_verdicts: dict[str, LayerVerdict]
    dominant_variables: list[str]
    bullish_chain: list[str]
    bearish_chain: list[str]
    forecast_hypotheses: list[ForecastHypothesis]
    missing_evidence: list[str]
    contradiction_report: list[str]
    dragging_layers: list[str]
    next_watch: list[str]
    ai_summary: str


def analyze_evidence_pack(
    api_key: str,
    pack: EvidencePack,
    *,
    time_windows: list[str] | None = None,
    mode: str = 'stock',
) -> EvidenceAnalysis:
    """Analyze only the supplied EvidencePack.

    api_key and mode are accepted for future LLM integration compatibility. The
    current implementation is deterministic and grounded in the pack.
    """
    _ = api_key, mode
    windows = time_windows or ['immediate', 'short', 'swing', 'mid', 'long']
    layer_verdicts = _layer_verdicts(pack)
    bullish_chain = _chain(pack, 'bullish')
    bearish_chain = _chain(pack, 'bearish')
    missing = [
        node.label for node in pack.nodes
        if node.type == 'missing_evidence' or node.forecast_role == 'missing'
    ]
    dominant_variables = _dominant_variables(pack)
    overall_direction, confidence = _overall_direction(pack)
    stage = _intel_stage(pack, bullish_chain, bearish_chain, missing, confidence)
    forecast_hypotheses = _forecast_hypotheses(pack, bullish_chain, bearish_chain, missing)

    time_window_verdicts = {}
    for window in windows:
        related = [
            node for node in pack.nodes
            if window in (node.time_windows or [])
            and node.type != 'missing_evidence'
            and node.analysis_weight > 0
        ]
        if not related:
            time_window_verdicts[window] = LayerVerdict(
                direction='unknown',
                confidence=0,
                summary='当前窗口缺少可采信证据。',
                time_window=window,
                drag_reason='证据不足',
            )
            continue
        direction, win_conf = _direction_from_nodes(related)
        time_window_verdicts[window] = LayerVerdict(
            direction=direction,
            confidence=win_conf,
            summary='；'.join((n.summary or n.label) for n in related[:3])[:220],
            dominant_variables=[n.label for n in sorted(
                related,
                key=lambda item: item.analysis_weight,
                reverse=True,
            )[:3]],
            expectation_gap=_first_non_unknown(n.expectation_gap for n in related),
            time_window=window,
        )

    next_watch = [f'补充{label.replace("证据缺失", "")}证据' for label in missing[:6]]
    if not next_watch:
        next_watch = ['观察现有证据是否获得持续确认']

    summary = _summary(pack, stage, overall_direction, confidence, dominant_variables, missing)
    return EvidenceAnalysis(
        overall_direction=overall_direction,
        confidence=confidence,
        intel_stage=stage,
        layer_verdicts=layer_verdicts,
        time_window_verdicts=time_window_verdicts,
        dominant_variables=dominant_variables,
        bullish_chain=bullish_chain,
        bearish_chain=bearish_chain,
        forecast_hypotheses=forecast_hypotheses,
        missing_evidence=missing,
        contradiction_report=list(pack.contradiction_report or []),
        dragging_layers=list(pack.dragging_layers or []),
        next_watch=next_watch,
        ai_summary=summary,
    )


def _layer_verdicts(pack: EvidencePack) -> dict[str, LayerVerdict]:
    verdicts: dict[str, LayerVerdict] = {}
    for layer, score in pack.layer_scores.items():
        verdicts[layer] = LayerVerdict(
            direction=str(score.get('direction') or 'unknown'),
            confidence=int(score.get('confidence') or 0),
            summary=str(score.get('summary') or ''),
            dominant_variables=list(score.get('dominant_variables') or []),
            expectation_gap=str(score.get('expectation_gap') or 'unknown'),
            time_window=str(score.get('time_window') or 'mixed'),
            drag_reason=str(score.get('drag_reason') or ''),
        )
    return verdicts


def _chain(pack: EvidencePack, direction: str) -> list[str]:
    nodes = [
        node for node in pack.nodes
        if node.direction == direction
        and node.type != 'missing_evidence'
        and node.analysis_weight > 0
    ]
    nodes.sort(key=lambda node: (node.analysis_weight, node.display_weight), reverse=True)
    return [
        f'{node.layer}：{node.label} - {(node.summary or node.label)[:100]}'
        for node in nodes[:6]
    ]


def _dominant_variables(pack: EvidencePack) -> list[str]:
    nodes = [
        node for node in pack.nodes
        if node.type != 'missing_evidence'
        and node.analysis_weight > 0
        and node.layer not in {'company'}
    ]
    nodes.sort(key=lambda node: (node.analysis_weight, node.display_weight), reverse=True)
    return [node.label for node in nodes[:6]]


def _overall_direction(pack: EvidencePack) -> tuple[str, int]:
    nodes = [
        node for node in pack.nodes
        if node.type != 'missing_evidence' and node.analysis_weight > 0
    ]
    if not nodes:
        return 'unknown', 0
    pos = sum(node.analysis_weight for node in nodes if node.direction == 'bullish')
    neg = sum(node.analysis_weight for node in nodes if node.direction == 'bearish')
    total = sum(node.analysis_weight for node in nodes)
    confidence = min(10, int(round(total * 1.5)))
    if pos and neg and abs(pos - neg) <= max(pos, neg) * 0.35:
        return 'mixed', confidence
    if pos > neg:
        return 'bullish', confidence
    if neg > pos:
        return 'bearish', confidence
    return 'neutral', confidence


def _intel_stage(
    pack: EvidencePack,
    bullish_chain: list[str],
    bearish_chain: list[str],
    missing: list[str],
    confidence: int,
) -> str:
    if pack.contradiction_report:
        return 'conflict'
    if bearish_chain and any(node.layer == 'risk' for node in pack.nodes if node.direction == 'bearish'):
        return 'risk'
    if not bullish_chain and not bearish_chain:
        return 'none' if missing else 'weak_watch'
    has_trade_confirmation = any(
        node.layer == 'trading_behavior' and node.analysis_weight >= 0.5
        for node in pack.nodes
    )
    has_company_or_financial = any(
        node.layer in {'company', 'financial', 'demand', 'cost', 'export', 'customer_supplier'}
        and node.analysis_weight >= 0.5
        for node in pack.nodes
    )
    if has_trade_confirmation and (bullish_chain or bearish_chain) and confidence >= 5:
        return 'fund_confirmed'
    if has_company_or_financial and confidence >= 5:
        return 'strong'
    if bullish_chain or bearish_chain:
        return 'candidate'
    return 'weak_watch'


def _forecast_hypotheses(
    pack: EvidencePack,
    bullish_chain: list[str],
    bearish_chain: list[str],
    missing: list[str],
) -> list[ForecastHypothesis]:
    hypotheses: list[ForecastHypothesis] = []
    if bullish_chain:
        hypotheses.append(ForecastHypothesis(
            title='正向证据可能继续发酵',
            direction='bullish',
            time_window='short',
            confidence=min(7, max(3, len(bullish_chain) + 2)),
            based_on=bullish_chain[:4],
            missing_or_risk=missing[:4],
            summary='该假设只来自当前正向证据链，仍需后续数据确认。',
        ))
    if bearish_chain:
        hypotheses.append(ForecastHypothesis(
            title='负向证据可能形成约束',
            direction='bearish',
            time_window='short',
            confidence=min(7, max(3, len(bearish_chain) + 2)),
            based_on=bearish_chain[:4],
            missing_or_risk=missing[:4],
            summary='该假设只来自当前负向证据链，不能外推为未提供事实。',
        ))
    return hypotheses[:3]


def _direction_from_nodes(nodes) -> tuple[str, int]:
    pos = sum(node.analysis_weight for node in nodes if node.direction == 'bullish')
    neg = sum(node.analysis_weight for node in nodes if node.direction == 'bearish')
    total = sum(node.analysis_weight for node in nodes)
    confidence = min(10, int(round(total * 2)))
    if pos and neg:
        return 'mixed', confidence
    if pos > neg:
        return 'bullish', confidence
    if neg > pos:
        return 'bearish', confidence
    return 'neutral', confidence


def _first_non_unknown(values) -> str:
    for value in values:
        text = str(value or 'unknown')
        if text != 'unknown':
            return text
    return 'unknown'


def _summary(pack, stage, direction, confidence, dominant_variables, missing) -> str:
    variables = '、'.join(dominant_variables[:3]) if dominant_variables else '暂无明确主导变量'
    miss = f'；缺失：{"、".join(missing[:3])}' if missing else ''
    return (
        f'{pack.subject_name} 当前情报阶段为 {stage}，总体方向 {direction}，'
        f'置信度 {confidence}/10；主导变量：{variables}{miss}。'
    )
