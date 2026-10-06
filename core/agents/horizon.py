"""Z 层 — 时间维度加权。

确定性逻辑（不调用 LLM，省 token）。
根据用户画像的 horizon 偏好，生成一段约束文本喂给 G 层决策，
让 G 层在输出 horizon_days 时遵守用户偏好。
"""
from __future__ import annotations

from typing import Any

from core.prediction_policy import FORCED_HORIZON_DAYS

# 不同 horizon 下偏向哪个 Agent 的报告
_HORIZON_FOCUS = {
    '短线（1-5天）': '技术面与资金流是首要依据，宏观和公司面权重较低',
    '中线（1-4周）': '宏观情报与公司面权重较高，技术面用于择时',
    '长线（1-12月）': '公司面和长期景气度是首要依据，短期技术信号不影响方向',
    '混合': '四维度均衡考虑，但首选有 3+ 维度共振的机会',
}


def _get_concentration_warning(code: str, sectors: list[str] | None) -> str:
    """检查虚拟持仓集中度，返回提示文字（无需提示时返回 ''）。

    注意：虚拟持仓功能已被追踪任务取代，此函数保留仅为向后兼容。
    优先级：
      1. 同 code 已有 open 持仓 → 重复加仓提示
      2. 与当前 sectors 有交集的 open 持仓 ≥2 只 → 板块集中度提示
      3. open 总数 ≥3 → 总仓位降 confidence 提示
    """
    if not code:
        return ''
    try:
        from core.cache import load_virtual_portfolio
        portfolio = load_virtual_portfolio() or []
        open_items = [p for p in portfolio if p.get('status') == 'open']

        same_code = [p for p in open_items if p.get('code') == code]
        if same_code:
            return (
                f'⚠️ 持仓提示：{code} 已有未结算的虚拟持仓'
                f'（{len(same_code)} 笔），建议 G 层标记 watch_only 避免重复加仓'
            )

        cur_secs = set(sectors or [])
        if cur_secs:
            overlap = []
            for p in open_items:
                p_secs = set(p.get('sectors') or [])
                shared = cur_secs & p_secs
                if shared:
                    overlap.append((p.get('code', '?'), shared))
            if len(overlap) >= 2:
                shared_names = sorted({s for _, ss in overlap for s in ss})
                codes = ', '.join(c for c, _ in overlap)
                return (
                    f'⚠️ 板块集中度：已有 {len(overlap)} 只同板块 open 持仓'
                    f'（{codes}；共享板块 {",".join(shared_names)}），'
                    f'建议 G 层降低 confidence 或标记 watch_only'
                )

        if len(open_items) >= 3:
            return (
                f'⚠️ 总仓位提示：虚拟持仓已有 {len(open_items)} 只未结算，'
                f'建议 G 层酌情降低 confidence'
            )
        return ''
    except Exception:
        return ''


def build_horizon_directive(
    profile: dict | None,
    code: str = '',
    sectors: list[str] | None = None,
) -> str:
    """生成时间维度约束文本（注入 G 层 user prompt）。

    Args:
        profile: 用户画像 dict（含 style.horizon）
        code: 当前分析标的代码（用于持仓集中度检查，可选）
        sectors: 当前标的所属板块列表（保留参数，目前未使用）

    Returns:
        中文指令文本，G 层会依此约束输出的 horizon_days 范围
    """
    text = (
        '【时间维度约束】\n'
        '产品定位：基于现状预测，固定复盘窗口。\n'
        f'  - horizon_days 必须固定 {FORCED_HORIZON_DAYS} 个交易日。\n'
        '  - 所有目标幅度、止损、进攻/防守条件都必须按这个三日窗口验证。\n'
        '  - 不允许因为趋势叙事、情绪或中期观点自动延长周期。\n'
        '  - 中期/行业/大盘判断只能作为背景，不得覆盖三日内可验证的技术与资金证据。'
    )
    warning = _get_concentration_warning(code, sectors)
    if warning:
        text += f'\n{warning}'
    return text

    focus = _HORIZON_FOCUS['短线（1-5天）']

    text = (
        f'【时间维度约束】\n'
        f'产品定位：超短线\n'
        f'  - horizon_days 必须在 2 ~ 5 个交易日内\n'
        f'  - 决策侧重：{focus}\n'
        f'  - 所有目标幅度/预测区间必须是该时间内技术上可达的，'
        f'若辩论结论指向更长周期，只提取其中 2~5 个交易日内可验证的部分'
    )

    warning = _get_concentration_warning(code, sectors)
    if warning:
        text += f'\n{warning}'
    return text
