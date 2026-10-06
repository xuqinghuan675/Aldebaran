"""G 层 — 最终决策。

输入：6 个 Agent 报告 + F 层辩论纪要 + Z 层时间约束 + 用户画像
输出：严格 JSON 预测（direction/confidence/entry_ref/target_pct/stop_pct/horizon_days/invalidation/reasoning）

使用 deepseek-flash (V4.1 Flash) thinking 模型，允许 8K 输出。
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from core.agents.base import build_cached_user_message, call_pro, agent_dict_to_text, STRICT_SOURCE_GROUNDING
from core.prediction_policy import (
    FORCED_HORIZON_DAYS,
    format_calibration_context,
    normalize_horizon_days,
)


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
        f"加仓条件：{opportunity.get('add_condition') or '—'}；"
        f"减仓条件：{opportunity.get('reduce_condition') or '—'}；"
        f"退出条件：{opportunity.get('exit_condition') or '—'}；"
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
    for item in candidates[:4]:
        if not isinstance(item, dict):
            continue
        cand_text.append(
            f"{item.get('strategy_name') or '—'}"
            f"/{item.get('stage') or '—'}"
            f"/score={item.get('match_score') if item.get('match_score') is not None else '—'}"
            f"/buy_ready={bool(item.get('buy_ready'))}"
        )
    knowledge = profile.get('strategy_knowledge') if isinstance(profile.get('strategy_knowledge'), list) else []
    return (
        f"选择策略：{profile.get('strategy_name') or '—'}；"
        f"策略家族：{profile.get('strategy_family') or '—'}；"
        f"阶段：{profile.get('stage') or '—'}；"
        f"buy_ready：{bool(profile.get('buy_ready'))}；"
        f"动作提示：{profile.get('action_hint') or '—'}；"
        f"触发条件：{'、'.join(map(str, profile.get('trigger_conditions') or [])) or '无'}；"
        f"缺口：{'、'.join(map(str, profile.get('missing_conditions') or [])) or '无'}；"
        f"失效：{profile.get('invalidation') or '—'}；"
        f"候选策略：{' | '.join(cand_text) or '无'}；"
        f"策略知识：{' | '.join(map(str, knowledge[:4])) or '无'}"
    )


_SYSTEM = """\
你是首席交易员，基于研究团队的辩论结论生成最终结构化预测。

以严格 JSON 格式输出，不含任何额外文字：
{
  "direction": "bullish" | "bearish" | "neutral",
  "confidence": 1-10整数（1=极低，10=极高），
  "user_suitability": "high" | "medium" | "low"（仅基于用户适配度评估，不影响 direction）,
  "final_action": "buy" | "sell" | "hold" | "watch_only" | "not_suitable",
  "category": "buy" | "avoid" | "watch" | "bullish_watch"（追踪契约分类，与机会等级硬映射）,
  "entry_trigger": 触发价（浮点；category=bullish_watch 必填，须与【确定性机会分层】的进攻位一致；其余为 null）,
  "fail_level": 失效价（浮点；category=bullish_watch 必填，须与【确定性机会分层】的防守位一致；category=avoid 必填，为当前价上方的关键阻力位，涨破即踏空，禁止填价格下方的支撑位；其余为 null）,
  "entry_ref": 入场参考价（浮点；direction=neutral 或 final_action=watch_only/not_suitable 时为 null）,
  "target_pct": 目标涨跌幅%（正数；direction=neutral 或 final_action=not_suitable 时为 null；bearish=预期跌幅；category=bullish_watch 时=触发后预计涨幅，基于触发价口径）,
  "stop_pct": 止损幅度%（正数；direction=neutral 或 final_action=not_suitable 时为 null）,
  "neutral_type": "neutral_range" | "neutral_conflict" | "neutral_low_data" | "neutral_wait_signal"（direction=neutral 时必填，否则为 null）,
  "expected_range_pct": [低端%, 高端%]（category=watch 或 neutral_range 时填写，仅作展示参考、不参与评分，如[-3,3]；否则为 null）,
  "horizon_days": 持仓周期交易日数（2~5 超短线，必须在时间维度约束范围内）,
  "invalidation": "让此预测失效的具体可判定条件（如大盘跌超N%、板块龙头破位等）",
  "final_rating": "strong_buy" | "buy" | "hold" | "sell" | "strong_sell",
  "opportunity_grade": "S" | "A" | "B" | "C" | "D",
  "opportunity_label": "S 已触发机会 / S 已触发但高风险 / A 临界机会 / B 持仓观察 / C 弱观察 / D 回避",
  "setup_phase": "已触发 / 高风险触发 / 临界未触发 / 候选未触发 / 弱候选 / 回避",
  "strategy_family": "pullback_low_absorb|breakout_momentum|oversold_rebound|event_catalyst_watch|trend_follow_momentum|etf_trend_follow|risk_avoid|none",
  "strategy_name": "本次最接近的策略名称，必须来自【确定性策略原型与知识】",
  "strategy_stage": "forming|triggered|failed|none",
  "buy_strategy": "final_action=buy 时必须填写采用的买入策略名称，否则 null",
  "strategy_reason": "选择或拒绝该策略的现实理由，必须引用触发/缺口/失效条件",
  "not_holding_plan": "未持仓用户的中文操作计划",
  "holding_plan": "已有持仓用户的中文操作计划",
  "add_condition": "允许加仓或重新买入的条件",
  "reduce_condition": "需要减仓的条件",
  "exit_condition": "需要退出的条件",
  "risk_reward": 风险收益比数字或 null,
  "attack_level": "进攻条件",
  "defense_level": "防守条件",
  "evidence_gaps": ["证据缺口1", "证据缺口2"],
  "thesis": "600-1000字执行型决策摘要：①承接F层辩论结论②五维市场证据如何收敛③引用Agent报告关键句④机会等级与当前能否买⑤未持仓/已持仓分别怎么做"
}

final_rating 映射：strong_buy=bullish+confidence≥8，buy=bullish+confidence≥5，hold=neutral或confidence<5，sell=bearish+confidence≥5，strong_sell=bearish+confidence≥8。

硬约束：
  - confidence 如实反映不确定性，F层裁决neutral时给≤5，五维市场共振才给≥7
  - horizon_days 落在【时间维度约束】指定范围内
  - stop_pct 优先以Agent4的ATR14为基础，下限≥3.0%（A股日内正常波动±2%，止损<3%会被噪音频繁触发；无ATR14时默认3-5%）
  - target_pct 与 stop_pct 盈亏比 ≥ 2.0（bullish/bearish 时；2:1 是交易最低标准，<2.0 不值得入场）
  - 标的为 ETF（代码 15x/16x/18x/50x/51x/52x/56x/58x）时：是一篮子低波动品种，不存在龙头/游资/打板，目标按指数波动给小，禁止个股龙头叙事与单家公司财报口径
  - thesis 必须明确引用F层裁决方向；G层方向与F层不一致需说明override理由
  - thesis 要写成执行型决策摘要，不要写成论文；允许 600-1000 字，但每段都必须服务于机会等级、进攻、防守或仓位动作
  - thesis 只写市场分析，禁止出现"用户偏好""不适合该用户"等用户画像内容
  - 数据严重不足时输出 direction=neutral, confidence=0, final_rating=hold
  - direction=neutral 时：entry_ref/target_pct/stop_pct 必须为 null
  - final_action=watch_only 且 direction≠neutral 时：entry_ref 为 null，但 target_pct/stop_pct 仍需给出（用于追踪验证）
  - watch_only 的目标、止损、进攻条件是触发后的验证计划，不是当前买入样本
  - 必须读取【确定性策略原型与知识】和 Agent4 的策略报告；final_action=buy 时，buy_strategy 必须从 strategy_candidates 中选择，strategy_stage 必须为 triggered，且 buy_ready=true
  - strategy_stage=forming/failed 或 buy_ready=false 时，不得输出 final_action=buy；只能输出 watch_only/hold/avoid，并在 strategy_reason/thesis 写明还差哪些触发条件
  - buy_strategy 是买入动作的契约字段：非 buy 时为 null；buy 时不得为空、不得写泛化词（如“短线策略”“看好买入”）
  - final_action=not_suitable 时：entry_ref/target_pct/stop_pct 必须为 null
  - thesis 中的操作建议措辞必须与 final_action 一致（如 watch_only 时禁止写"建议卖出/买入"，应写"建议观望"）
  - user_suitability 仅描述契合度，不得影响 direction 和市场方向判断
  - 目标用户为A股散户，无融券做空能力；bearish方向的final_action应为sell/hold/watch_only（减仓/观望），不要给出做空建议
  - 候选形态不等于买点：若确定性短线模式为“候选未触发”，final_action 必须为 watch_only，entry_ref 必须为 null，但 direction 可按机会等级填 bullish（A/B 临界/持仓观察方向偏多）或 neutral（C 弱观察方向不明）
  - 若确定性短线模式为“回避/失败”，未持仓用户不得输出 sell，应输出 watch_only（不买/等待）
  - 若确定性短线模式为“回避/失败”，即使用户有持仓，也必须在 thesis 写明“禁止新增买入，仅做持仓风控/等待重评”
  - 若确定性短线模式没有失效价或技术画像不可用，invalidation 禁止编造具体价格位，只能使用“技术数据恢复后重评”“主力资金连续流出”“事件未获量价验证”等可观察条件
  - 若用户无持仓且 direction=bearish，final_action 必须为 watch_only，不要显示“卖出”
  - 只有确定性短线模式为“已触发”或明确强趋势时，才允许 final_action=buy
  - 当 strategy_family=trend_follow_momentum 且 strategy_stage=triggered 且 buy_ready=true（主线契合已成立、机会等级S）时：缩量上涨（量比<1）不构成单独否决理由，趋势延续买点不要求放量（放量常见于见顶）；只要未跌破MA5/失效价、主力资金未转为持续流出、盈亏比≥2、无F层否决，应给出 final_action=buy 轻仓跟随，不要以“等待放量”为由降级为观望。**强约束：高风险触发（主力净流入仅单日/未满3日连续）不是降级理由——单日主力净流入即满足轻仓买入门槛；“主力连续性未确认”“KDJ/RSI超买”“距前高有空间”均不得作为把 S+buy_ready 龙头降为 bullish_watch/观望的借口。此时 final_action 必须=buy（轻仓），靠失效价控风险而非靠观望；确需谨慎只能体现在仓位轻、不能体现在不买。**
  - 当 strategy_family=etf_trend_follow 且 strategy_stage=triggered 且 buy_ready=true 时：标的为 ETF（一篮子/跟踪指数），不存在主线龙头/游资/打板；买点成立条件为板块趋势延续、相对强势或资金净流入之一，未跌破MA5/失效价且盈亏比≥2、无F层否决时给 final_action=buy 轻仓跟随。thesis 用"板块趋势延续/指数贝塔/相对强势"措辞，禁止"主线契合/龙头/主力抱团"。目标按指数低波动给小（≤8%）。
  - 必须服从确定性机会分层：D=不买/回避；A/B/C=未持仓不买，只写等待条件；S高风险=不得写重仓追高，只能轻仓试错或持仓风控
  - opportunity_grade/opportunity_label/setup_phase/risk_reward/attack_level/defense_level 必须与【确定性机会分层】一致，不允许因为F层谨慎或自身判断而把S降成C、把A降成D、或改写阶段
  - 若F层裁决中的“机会等级”与【确定性机会分层】冲突，以【确定性机会分层】为准；thesis 中可写“F层认为买入积极性下降，但确定性等级仍为S高风险”
  - “当前不宜追高”只能影响 final_action、not_holding_plan、holding_plan，不能改写 opportunity_grade
  - direction 表达的是市场方向倾向，不是交易动作；方向倾向 ≠ 交易动作：
    A 临界机会 → direction=bullish（结构偏多）+ final_action=watch_only（但未触发不买）+ final_rating=hold
    B 持仓观察 → direction=bullish（结构偏多）+ final_action=watch_only（候选未触发）+ final_rating=hold
    C 弱观察 → direction=neutral（方向不明/弱候选/低数据/证据冲突），不得包装成看多
    D 回避 → direction=bearish（看空回避），禁止 bullish
    S 已触发（无论“普通触发”还是“高风险触发”）→ direction 一律 bullish（结构性偏多），禁止输出 neutral；“高风险”只影响 final_action（buy vs watch_only）与仓位（轻仓试错/持仓风控），绝不改变 direction
  - direction=neutral 仅用于：方向不明、C 弱观察、数据不足、证据冲突、退潮/冰点环境；不要在 A/B 机会上填 neutral
  - direction=bullish + final_action=watch_only 是合法且常见的组合，表示“偏多但未触发/不宜追”，thesis 必须写清“偏多但不能直接买”
  - category 与机会等级硬映射：S 已触发且 final_action=buy → buy；S 高风险触发（watch_only）/A 临界/B 持仓观察 → bullish_watch；C 弱观察 → watch；D 回避 → avoid
  - category=watch 时 expected_range_pct 仍填写（仅展示参考，不参与评分）；category=bullish_watch 时 target_pct 必填=触发后预计涨幅（基于触发价口径）且 entry_trigger/fail_level 必填；category=avoid 时 target_pct=预测跌幅且 fail_level 必填
  - 幅度-时间匹配：target_pct/预测跌幅 必须是 horizon_days（2~5 个交易日）内技术上可达的幅度，以 ATR14 与近期实际波动为锚（N 日累计可达幅度 ≈ ATR14×N×1~1.5 倍），禁止给出该时间内无法到达的目标
  - 若 2~5 天可达幅度撑不起盈亏比 2.0，不得虚高 target_pct，应降级 category（buy → bullish_watch/watch）并在 thesis 写明“空间不足”
  - 中性观望（category=watch / direction=neutral）评分以散户视角只罚踏空：审判日实际涨幅 ≤+4% 即判对（A：下跌=躲过下跌、横盘/小涨=判断合理），涨 +4%~+6% 判 D（踏空）、涨 >+6% 判 F（大幅踏空），下跌不扣分。给 neutral/watch 等于预判该标的 horizon 内上涨空间 <4%；若你判断有 ≥4% 上行空间，必须给 bullish（buy/bullish_watch），不得用中性观望回避，否则审判日会被判踏空（D/F）。expected_range_pct 仅作展示、不再约束宽度
  - thesis 必须同时写清楚：机会等级、进攻条件、防守条件、未持仓策略、已持仓策略
  - invalidation 必须是单一可观测条件或用“或”连接的独立条件，禁止用“且”组合逻辑矛盾的条件
  - 引用财务指标时，报告期口径必须与基本面快照标注的"报告期口径"完全一致，thesis 中据实标明该报告期，不得改用其他报告期称谓，更不可当作年度数据解读
  - 禁止自行将单期 ROE 乘以倍数年化；若数据源已提供"ROE年化估算"可引用，否则只引用原始单期值
  - 技术行为标签只能作为“疑似”证据引用，禁止写成“主力确认/游资确认/洗盘完成/出货确认”
  - 当资金流 Agent 标注“仅1日数据/趋势未确认/历史样本不足”时，thesis 禁止构造“连续N日/近5日/多日主力净流入”等多日趋势主张，只能表述为“单日资金迹象，趋势待量价验证”
  - 公开资金证据主要听取资金流向 Agent；技术 Agent 中的公开证据简述只能作为旁证
  - 大盘情绪阶段估计只允许降低 confidence 或转为 watch_only，不得因为 main_up/repair 单独提高 confidence\
"""

_SYSTEM = _SYSTEM + "\n" + STRICT_SOURCE_GROUNDING + f"""

【固定3日与Agent校准硬规则】
- horizon_days 必须输出 {FORCED_HORIZON_DAYS}，不得输出 2、4、5 或按情绪延长周期。
- 事件/新闻中性且缺少三日内新鲜情报时，按“情报缺口”处理，不等于利空。
- 少于3个交易日的资金流 bearish 只按短窗噪声降权，不能构造多日持续流出叙事。
- 技术面负责三日方向，过热/超买只进入仓位、止损和风险表达，不得直接否定已触发趋势。
- 基本面偏空在技术过热且情报不足时可以触发保护性降档；大盘/行业只作背景，不能单独触发买入。
"""

_STABLE_PREFIX = """\
[G 层 · 首席交易员最终决策]
请基于下方研究团队的辩论结论生成严格 JSON 预测。\
"""


def _build_variable_data(
    code: str,
    name: str,
    kind: str,
    current_price: float | None,
    macro_report: str,
    user_report: str,
    company_report: str,
    technical_report: str,
    debate_text: str,
    horizon_directive: str,
    profile_summary: str = '',
    news_report=None,
    fundflow_report=None,
    trade_setup=None,
    opportunity_profile=None,
    strategy_profile=None,
    cognition=None,
    calibration_context=None,
) -> str:
    obj_label = f'{name}（{code}）' if code else name
    parts = [
        '【决策对象】',
        f'对象：{obj_label}（{kind}）  日期：{date.today().strftime("%Y-%m-%d")}',
    ]
    if current_price:
        parts.append(f'当前价：{current_price:.3f} 元')
    parts.append('')

    if profile_summary:
        parts.append(profile_summary)
        parts.append('')

    if trade_setup:
        parts.extend([
            '【确定性短线交易模式】',
            _compact_setup_text(trade_setup),
            '',
        ])
    if opportunity_profile:
        parts.extend([
            '【确定性机会分层】',
            _compact_opportunity_text(opportunity_profile),
            '',
        ])
    if strategy_profile:
        parts.extend([
            '【确定性策略原型与知识】',
            _compact_strategy_text(strategy_profile),
            '',
        ])

    if cognition:
        parts.extend([
            '【用户本次持仓状态】',
            str(cognition),
            '',
        ])

    parts.append(horizon_directive)
    parts.append('')

    if calibration_context:
        parts.extend([
            '【Agent校准与情报覆盖】',
            format_calibration_context(calibration_context),
            '',
        ])

    parts.extend([
        '【F 层辩论裁决】',
        debate_text or '（辩论未能完成，请直接基于五维市场报告判断）',
        '',
        '【Agent1 市场环境】',
        agent_dict_to_text(macro_report) or '（缺失）',
        '',
        '【Agent3 基本面】',
        agent_dict_to_text(company_report) or '（缺失）',
        '',
        '【Agent4 技术面】',
        agent_dict_to_text(technical_report) or '（缺失）',
        '',
        '【Agent5 资金流向】',
        agent_dict_to_text(fundflow_report) or '（缺失）',
        '',
        '【Agent6 事件催化】',
        agent_dict_to_text(news_report) or '（缺失）',
        '',
        '【用户适配度评估（仅影响 user_suitability/final_action，不参与方向判断）】',
        agent_dict_to_text(user_report) or '（缺失）',
        '',
        '请按 system 中指定的 JSON 格式输出最终预测：',
    ])
    return '\n'.join(parts)


def _parse_prediction_json(content: str) -> dict | None:
    """从响应解析 JSON。复用 predictor._parse_prediction_json 的兜底逻辑。"""
    if not content:
        return None
    def _normalize(data: dict) -> dict:
        # thesis / reasoning 互为兜底
        if not data.get('thesis') and data.get('reasoning'):
            data['thesis'] = data['reasoning']
        elif not data.get('reasoning') and data.get('thesis'):
            data['reasoning'] = data['thesis']
        data.setdefault('final_rating', 'hold')
        data.setdefault('user_suitability', 'medium')
        data.setdefault('final_action', 'hold')
        data.setdefault('opportunity_grade', None)
        data.setdefault('opportunity_label', None)
        data.setdefault('setup_phase', None)
        data.setdefault('not_holding_plan', None)
        data.setdefault('holding_plan', None)
        data.setdefault('add_condition', None)
        data.setdefault('reduce_condition', None)
        data.setdefault('exit_condition', None)
        data.setdefault('risk_reward', None)
        data.setdefault('attack_level', None)
        data.setdefault('defense_level', None)
        data.setdefault('evidence_gaps', [])
        data.setdefault('strategy_family', None)
        data.setdefault('strategy_name', None)
        data.setdefault('strategy_stage', None)
        data.setdefault('buy_strategy', None)
        data.setdefault('strategy_reason', None)
        data.setdefault('neutral_type', None)
        data.setdefault('expected_range_pct', None)
        data.setdefault('entry_trigger', None)
        data.setdefault('fail_level', None)
        if data.get('category') not in ('buy', 'avoid', 'watch', 'bullish_watch'):
            data['category'] = None
        if not data.get('category'):
            if data.get('final_action') == 'buy':
                data['category'] = 'buy'
            elif data.get('direction') == 'bullish' and data.get('final_action') == 'watch_only':
                data['category'] = 'bullish_watch'
            elif data.get('direction') == 'bearish':
                data['category'] = 'avoid'
            else:
                data['category'] = 'watch'
        _no_entry = (
            data.get('direction') == 'neutral'
            or data.get('final_action') in ('watch_only', 'not_suitable')
        )
        if _no_entry:
            data['entry_ref'] = None
        if data.get('direction') == 'neutral' or data.get('final_action') == 'not_suitable':
            data['target_pct'] = None
            data['stop_pct'] = None
        # LLM 可能返回负数，强制取绝对值
        for k in ('target_pct', 'stop_pct'):
            if isinstance(data.get(k), (int, float)):
                data[k] = abs(data[k])
        # 非 neutral 方向若 LLM 漏填 target/stop，用保守默认值兜底（追踪系统需要）
        if data.get('direction') in ('bullish', 'bearish') and data.get('final_action') != 'not_suitable':
            if not data.get('target_pct'):
                data['target_pct'] = 5.0
            if not data.get('stop_pct'):
                data['stop_pct'] = 3.0
            # stop_pct 下限硬约束：A股日内噪音±2%，止损<3%无意义
            s = data.get('stop_pct', 0) or 0
            if s > 0 and s < 3.0:
                data['stop_pct'] = 3.0
                s = 3.0
            # 盈亏比 ≥ 2.0 硬约束：LLM 经常违反，代码兜底
            t = data.get('target_pct', 0) or 0
            if s > 0 and t / s < 2.0:
                data['target_pct'] = round(s * 2.0, 1)
        data['horizon_days'] = normalize_horizon_days(data.get('horizon_days'))
        return data

    try:
        data = json.loads(content)
        if isinstance(data, dict) and 'direction' in data:
            return _normalize(data)
    except (json.JSONDecodeError, ValueError):
        pass

    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', content, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict) and 'direction' in data:
                return _normalize(data)
        except (json.JSONDecodeError, ValueError):
            pass

    # 兜底：正则抽取关键字段
    def _ex(p, t=str, d=None):
        mm = re.search(p, content)
        if mm:
            try:
                return t(mm.group(1))
            except (ValueError, TypeError):
                return d
        return d

    direction = _ex(r'"direction"\s*:\s*"(\w+)"')
    if direction in ('bullish', 'bearish', 'neutral'):
        thesis = _ex(r'"thesis"\s*:\s*"([^"]+)"', str, '')
        reasoning = _ex(r'"reasoning"\s*:\s*"([^"]+)"', str, '')
        is_neutral = direction == 'neutral'
        return _normalize({
            'direction': direction,
            'confidence': _ex(r'"confidence"\s*:\s*(\d+)', int, 0),
            'user_suitability': _ex(r'"user_suitability"\s*:\s*"(\w+)"', str, 'medium'),
            'final_action': _ex(r'"final_action"\s*:\s*"(\w+)"', str, 'hold'),
            'category': _ex(r'"category"\s*:\s*"(\w+)"'),
            'entry_trigger': _ex(r'"entry_trigger"\s*:\s*([\d.]+)', float),
            'fail_level': _ex(r'"fail_level"\s*:\s*([\d.]+)', float),
            'entry_ref': None if is_neutral else _ex(r'"entry_ref"\s*:\s*([\d.]+)', float),
            'target_pct': None if is_neutral else _ex(r'"target_pct"\s*:\s*([\d.]+)', float, 5.0),
            'stop_pct': None if is_neutral else _ex(r'"stop_pct"\s*:\s*(-?[\d.]+)', float, 2.5),
            'neutral_type': _ex(r'"neutral_type"\s*:\s*"([^"]+)"', str) if is_neutral else None,
            'expected_range_pct': None,
            'horizon_days': _ex(r'"horizon_days"\s*:\s*(\d+)', int, 3),
            'invalidation': _ex(r'"invalidation"\s*:\s*"([^"]+)"', str, ''),
            'final_rating': _ex(r'"final_rating"\s*:\s*"(\w+)"', str, 'hold'),
            'strategy_family': _ex(r'"strategy_family"\s*:\s*"([^"]+)"', str),
            'strategy_name': _ex(r'"strategy_name"\s*:\s*"([^"]+)"', str),
            'strategy_stage': _ex(r'"strategy_stage"\s*:\s*"([^"]+)"', str),
            'buy_strategy': _ex(r'"buy_strategy"\s*:\s*"([^"]+)"', str),
            'strategy_reason': _ex(r'"strategy_reason"\s*:\s*"([^"]+)"', str),
            'thesis': thesis or reasoning,
            'reasoning': reasoning or thesis,
        })
    return None


def run_decision(
    api_key: str,
    code: str,
    name: str,
    kind: str,
    current_price: float | None,
    macro_report: str,
    user_report: str,
    company_report: str,
    technical_report: str,
    debate_text: str,
    horizon_directive: str,
    profile_summary: str = '',
    news_report=None,
    fundflow_report=None,
    trade_setup=None,
    opportunity_profile=None,
    strategy_profile=None,
    cognition=None,
    calibration_context=None,
    **_: Any,
) -> tuple[dict | None, str, str]:
    """运行 G 层。

    Returns:
        (prediction_dict, reasoning_chain, error_msg)
        失败时 prediction_dict 为 None。
    """
    variable = _build_variable_data(
        code, name, kind, current_price,
        macro_report, user_report, company_report, technical_report,
        debate_text, horizon_directive, profile_summary,
        news_report, fundflow_report, trade_setup, opportunity_profile, strategy_profile, cognition,
        calibration_context,
    )
    user_msg = build_cached_user_message(_STABLE_PREFIX, variable)

    # G 层用 response_format=json_object，确保模型输出 JSON
    # 防截断：max_tokens=16000（思考 token + ~1100 token JSON + thesis 留足空间）
    # 防超时：timeout=180（思考模型 + 长输出常超 90s）
    # 解析失败/空返回时最多重试 1 次，再走正则兜底
    from core.agents.base import call_llm
    content, reasoning, prediction, _usage = '', '', None, None
    for _attempt in range(2):
        content, reasoning, _usage = call_llm(
            _SYSTEM, user_msg, api_key,
            model='deepseek-flash',
            max_tokens=16000,
            temperature=0.2,
            response_format={'type': 'json_object'},
            retries=1,
            timeout=180,
        )
        if content:
            prediction = _parse_prediction_json(content)
            if prediction:
                break

    if not content:
        api_error = (_usage or {}).get('error')
        if api_error:
            return None, reasoning, f'G 层调用失败（{api_error}）'
        finish_reason = (_usage or {}).get('finish_reason') or 'unknown'
        reasoning_tokens = (
            ((_usage or {}).get('completion_tokens_details') or {}).get('reasoning_tokens')
        )
        return (
            None, reasoning,
            f'G 层调用失败（无返回内容；finish_reason={finish_reason}；'
            f'reasoning_tokens={reasoning_tokens}）',
        )
    if not prediction:
        finish_reason = (_usage or {}).get('finish_reason') or 'unknown'
        return None, reasoning, f'G 层结构化结果解析失败（finish_reason={finish_reason}）'

    return prediction, reasoning, ''
