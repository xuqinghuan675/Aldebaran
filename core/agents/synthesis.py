"""F 层 — 多空辩论裁决。

借鉴 TradingAgents-CN 的 bull/bear/manager 三段论，但用单个 V4.1 Flash 调用完成内部辩论，
省 token 又能利用 V4.1 Flash thinking 模型的多步推理能力。

输入：5 个市场 Agent 的报告（macro/company/news/technical/fundflow）
输出：结构化辩论纪要（看多论据 / 看空论据 / 裁决倾向 / 置信度初值）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from core.agents.base import build_cached_user_message, call_pro, agent_dict_to_text, STRICT_SOURCE_GROUNDING
from core.prediction_policy import format_calibration_context


def _compact_setup_text(setup) -> str:
    if not isinstance(setup, dict) or not setup:
        return '（未提供）'
    setup_map = {
        'trend_breakout': '趋势突破',
        'box_breakout': '箱体突破',
        'pullback_buy': '上升回踩',
        'oversold_rebound': '超跌反弹',
        'event_momentum': '事件催化',
        'downtrend_avoid': '下跌回避',
        'trend_continuation': '强趋势延续',
    }
    status_map = {
        'candidate': '候选未触发',
        'triggered': '已触发',
        'failed': '已失败',
        'avoid': '回避',
    }
    risks = '、'.join(setup.get('risk_flags') or []) or '无'
    return (
        f"模式：{setup_map.get(setup.get('setup_name'), setup.get('setup_name') or '—')}；"
        f"状态：{status_map.get(setup.get('status'), setup.get('status') or '—')}；"
        f"触发：{setup.get('entry_trigger') or '—'}；"
        f"失效：{setup.get('fail_level') or '—'}；"
        f"目标：{setup.get('target_level') or '—'}；"
        f"仓位提示：{setup.get('position_hint') or '—'}；"
        f"风险：{risks}"
    )


def _compact_opportunity_text(opportunity) -> str:
    if not isinstance(opportunity, dict) or not opportunity:
        return '（未提供）'
    gaps = '、'.join(opportunity.get('evidence_gaps') or []) or '无'
    risks = '、'.join(opportunity.get('risk_flags') or []) or '无'
    return (
        f"机会等级：{opportunity.get('opportunity_label') or '—'}；"
        f"阶段：{opportunity.get('setup_phase') or '—'}；"
        f"风险收益比：{opportunity.get('risk_reward') if opportunity.get('risk_reward') is not None else '—'}；"
        f"未持仓：{opportunity.get('not_holding_plan') or '—'}；"
        f"已持仓：{opportunity.get('holding_plan') or '—'}；"
        f"进攻：{opportunity.get('attack_level') or '—'}；"
        f"防守：{opportunity.get('defense_level') or '—'}；"
        f"证据缺口：{gaps}；"
        f"风险：{risks}"
    )


def _compact_strategy_text(profile) -> str:
    if not isinstance(profile, dict) or not profile:
        return '（未提供）'
    candidates = profile.get('strategy_candidates') if isinstance(profile.get('strategy_candidates'), list) else []
    cand_text = []
    for item in candidates[:3]:
        if not isinstance(item, dict):
            continue
        cand_text.append(
            f"{item.get('strategy_name') or '—'}"
            f"/{item.get('stage') or '—'}"
            f"/score={item.get('match_score') if item.get('match_score') is not None else '—'}"
            f"/buy_ready={bool(item.get('buy_ready'))}"
        )
    knowledge = profile.get('strategy_knowledge') if isinstance(profile.get('strategy_knowledge'), list) else []
    missing = '、'.join(map(str, profile.get('missing_conditions') or [])) or '无'
    triggers = '、'.join(map(str, profile.get('trigger_conditions') or [])) or '无'
    return (
        f"选择策略：{profile.get('strategy_name') or '—'}；"
        f"家族：{profile.get('strategy_family') or '—'}；"
        f"阶段：{profile.get('stage') or '—'}；"
        f"buy_ready：{bool(profile.get('buy_ready'))}；"
        f"当前匹配：{'、'.join(map(str, profile.get('current_match') or [])) or '无'}；"
        f"触发：{triggers}；"
        f"缺口：{missing}；"
        f"失效：{profile.get('invalidation') or '—'}；"
        f"候选：{' | '.join(cand_text) or '无'}；"
        f"知识：{' | '.join(map(str, knowledge[:3])) or '无'}"
    )


_SYSTEM = """\
你是首席研究员（Research Manager），主持多空对抗式辩论并做出裁决。

辩论规则（对抗式，禁止各说各话）：
  1. 看多方：基于五份市场报告，提出 3-4 条最有力的看多论据，每条标注来源 Agent 和具体数据
  2. 看空方：逐条质疑——每条质疑必须以"质疑第N条："开头，指出该论据的逻辑漏洞、数据缺陷或时效性问题。禁止使用"市场存在不确定性""需关注政策风险"等无指向空话
  3. 看多方回应：对每条质疑做有针对性的简短辩护（通常 40-80 字），不能回避问题
  4. 裁决：逐条判定胜负，综合决定方向

短线裁决纪律：
  - 本产品定位 2-5 个交易日超短线：方向裁决与置信、机器裁决行均以该周期为基准；中期趋势仅作背景，不得用 5-15 日中期逻辑覆盖 2-5 日短线结论。
  - 必须区分 1-3 日短线反弹 与 5-15 日中期趋势，不得把二者混成一个方向。
  - 候选形态不等于买点；只有触发价成立才可把短线机会判为可交易。
  - 若技术面显示超跌反弹候选但中期空头，应裁决为“短线候选/中期偏空”，而非简单看多。
  - 若确定性短线模式为“回避/失败”或技术画像不可用，短线裁决不得看多，必须判为中性或看空，置信度初值最高4。
  - 若新闻利好缺少价格、量能、资金验证，只能作为“事件候选”，不得压倒技术不可交易结论。
  - 必须把“机会质量”和“当前能否买”分开：A临界机会可以有价值，但仍然不等于买点；S已触发也要检查过热、换手、事件反证和风险收益比。
  - 【确定性机会分层】是系统规则锚点，不是辩论双方的意见。裁决可以降低“当前买入积极性”，但不得改写机会等级。
  - 若确定性机会分层为“S已触发但高风险”，输出的机会等级必须仍为“S已触发但高风险”；可裁决为“中性/谨慎”，但不得降成C弱观察或候选未触发。
  - 若你认为S高风险不适合追高，应写“机会等级S高风险，当前未持仓不重仓追高/等待回踩”，而不是把已触发机会改成弱观察。
  - 多方不能只说“快突破了”，必须说明还差哪个触发条件；空方不能只说“保守”，必须指出具体失效条件或证据缺口。
  - 技术行为标签只能按“疑似启动/疑似吸筹/疑似洗盘/疑似出货/疑似破位”引用，不得改写为确定结论。
  - 公开资金证据以资金流向 Agent 的解释为准；技术面中的公开证据简述只作旁证。
  - 大盘情绪阶段估计只允许降低裁决置信度，不得因为 main_up/repair 单独提高置信度。
  - 若资金流 Agent 标注“仅1日数据/趋势未确认/历史样本不足”，debate 各方不得把1日资金放大为“连续N日/近5日主力净流入”等多日趋势，只能按“单日资金迹象、趋势未确认”引用。
  - 涉及占比/比例/百分比时，优先引用 Agent 已给出的数值；禁止自行换算占比。若输入未直接给出该百分比且你无法逐步核算，宁可只写原始金额/成交额，不要写出百分比。

输出格式（使用中文，控制总字数 1500-2500 字；要充分但不要堆重复套话）：

【看多论据】
1. [来源：AgentX] 论据内容...
2. ...

【看空质疑】
质疑第1条：针对论据1的具体漏洞...
质疑第2条：...

【看多回应】
回应质疑1：...
回应质疑2：...

【裁决】
方向：看多 / 看空 / 中性
逐条判定：
  第1条→多方胜（理由...）/ 空方胜（理由...）
  第2条→...
胜方理由（≤160 字）：...
置信度初值（1-10 整数）：N
关键不确定因素（≤120 字）：...
机会等级：必须抄写【确定性机会分层】给出的等级；若为S高风险，写“S已触发但高风险”
进攻条件：...
防守条件：...
最大反证：...

【机器裁决】（最后必须单独一行，严格格式，供程序解析；方向与上文【裁决】方向一致，看多=bullish/看空=bearish/中性=neutral，不得矛盾）
裁决:<bullish|bearish|neutral> 置信:<1-10整数>

裁决原则：
  - 若看空方对某条论据无法提出有效质疑，该条自动判多方胜
  - 质疑必须有具体指向，泛泛而谈的空话质疑视为无效
  - 若 Agent 报告之间出现冲突，必须在逐条判定中明确指出并说明取舍
  - 综合方向基于各条胜负，而非论据数量\
"""

_SYSTEM = _SYSTEM + "\n" + STRICT_SOURCE_GROUNDING

_STABLE_PREFIX = """\
[F 层 · 多空对抗辩论]
请基于下方五份市场分析报告，主持对抗式辩论并裁决——看多方立论，看空方逐条质疑，看多方回应，最后逐条判定。\
"""


def _build_variable_data(
    code: str,
    name: str,
    kind: str,
    macro_report,
    company_report,
    technical_report,
    news_report=None,
    fundflow_report=None,
    trade_setup=None,
    opportunity_profile=None,
    strategy_profile=None,
    calibration_context=None,
) -> str:
    obj_label = f'{name}（{code}）' if code else name
    parts = [
        '【辩论对象】',
        f'对象：{obj_label}（{kind}）  日期：{date.today().strftime("%Y-%m-%d")}',
        '',
        '【Agent1 市场环境报告】',
        agent_dict_to_text(macro_report) or '（缺失，请在辩论中标注）',
        '',
        '【Agent3 基本面报告】',
        agent_dict_to_text(company_report) or '（缺失，请在辩论中标注）',
        '',
        '【Agent4 技术面报告】',
        agent_dict_to_text(technical_report) or '（缺失，请在辩论中标注）',
        '',
        '【Agent5 资金流向报告】',
        agent_dict_to_text(fundflow_report) or '（缺失，请在辩论中标注）',
        '',
        '【Agent6 事件催化报告】',
        agent_dict_to_text(news_report) or '（缺失，请在辩论中标注）',
        '',
        '【确定性短线交易模式】',
        _compact_setup_text(trade_setup),
        '',
        '【确定性机会分层】',
        _compact_opportunity_text(opportunity_profile),
        '',
        '【确定性策略原型与知识】',
        _compact_strategy_text(strategy_profile),
        '',
        '请按系统提示的输出格式输出辩论纪要：',
    ]
    if calibration_context:
        parts[3:3] = [
            '【Agent校准与情报覆盖】',
            format_calibration_context(calibration_context),
            '',
        ]
    return '\n'.join(parts)


def run_debate(
    api_key: str,
    code: str,
    name: str,
    kind: str,
    macro_report: str,
    company_report: str,
    technical_report: str,
    news_report=None,
    fundflow_report=None,
    trade_setup=None,
    opportunity_profile=None,
    strategy_profile=None,
    calibration_context=None,
    **_: Any,
) -> tuple[str, str]:
    """运行 F 层辩论。

    Returns:
        (debate_text, reasoning) — debate_text 是结构化辩论纪要，
        reasoning 是 pro 模型的 thinking 链（可空）
    """
    variable = _build_variable_data(
        code, name, kind, macro_report, company_report, technical_report,
        news_report, fundflow_report, trade_setup, opportunity_profile, strategy_profile,
        calibration_context,
    )
    user_msg = build_cached_user_message(_STABLE_PREFIX, variable)
    content, reasoning = call_pro(_SYSTEM, user_msg, api_key, max_tokens=5200, temperature=0.1)
    return (content or '（多空辩论未能完成）'), reasoning
