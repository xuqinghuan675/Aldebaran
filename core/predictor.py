"""预测引擎 — 针对个股或板块生成结构化可判定预测。

v3.0 架构（5 Agent + F 多空辩论 + Z 时间维度 + G 决策）：
  Agent1  市场环境分析师    deepseek-flash (V4.1 Flash) thinking  ┐
  Agent3  基本面分析师      deepseek-flash (V4.1 Flash) thinking  │
  Agent4  技术面分析师      deepseek-flash (V4.1 Flash) thinking
  Agent5  资金流向分析师    deepseek-flash (V4.1 Flash) thinking  │
  Agent6  事件催化分析师    deepseek-flash (V4.1 Flash) thinking  ┘
  F 层    多空辩论裁决      deepseek-flash    串行
  Z 层    时间维度约束      确定性（无 LLM）   注入 horizon directive
  G 层    最终决策          deepseek-flash (V4.1 Flash) thinking → 严格 JSON

入口：predict_unified()  5 Agent + F/Z/G 编排

落盘：
  ~/.aldebaran/cache/ai_predictions.json
  ~/.aldebaran/cache/reasoning/{code}.json
  data/analysis_memory/{code}.md  （RAG 闭环）

DeepSeek prompt caching 设计：每个 Agent 的 system_prompt 字节级稳定，
user message = [稳定前缀（任务说明 + profile + memory）] + [变化数据]，
价格以 DeepSeek 官方 pricing 页面为准；pro/flash 命中 prompt cache 后输入成本会显著降低。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path

# 6 位股票代码精确匹配（数字边界 lookaround；不能用 \b，中文相邻会失效）
_CODE_RE = re.compile(r'(?<!\d)(\d{6})(?!\d)')


from core.paths import CACHE_DIR as _BASE_CACHE
from core.tracking_display import reconcile_view_to_category
from core.board_rules import is_etf
from core.prediction_policy import (
    FORCED_HORIZON_DAYS,
    build_agent_calibration_context,
    build_intel_coverage,
    normalize_horizon_days,
)
_PREDICTIONS_CACHE = _BASE_CACHE / 'ai_predictions.json'
# 预测缓存读-改-写锁：批量并发分析时多个 worker 线程会同时更新 ai_predictions.json，
# 加锁避免读改写竞态丢条目（reasoning 为按代码分文件、analysis_memory 自带锁，无需额外保护）。
_PRED_CACHE_LOCK = threading.Lock()
_REASONING_CACHE_DIR = _BASE_CACHE / 'reasoning'
_REASONING_TTL_SECONDS = 4 * 3600  # 推理层 4h 缓存
_REASONING_CACHE_VERSION = '2026-06-15-contract-grounding-v3'



def _add_trading_days(n: int, start: date | None = None) -> str:
    """从 start（默认今日）向后数 n 个 A 股交易日，返回 ISO 日期字符串。

    代理到 core.trade_calendar.add_trading_days，正确跳过周末+法定节假日。
    日历数据不可用时自动降级为纯周末跳过。
    """
    from core.trade_calendar import add_trading_days
    return add_trading_days(n, start)


def _apply_prediction_policy_metadata(
    prediction: dict,
    context: dict | None,
    stock_news: list[dict] | None,
    intel_events: list | None,
    agent_reports: dict | None = None,
    calibration_context: dict | None = None,
) -> dict:
    """Attach fixed-horizon and intel/agent calibration metadata to a prediction."""
    if not isinstance(prediction, dict):
        return {}
    context = context or {}
    stock_news = stock_news or []
    intel_events = intel_events or []
    if calibration_context is None and agent_reports:
        calibration_context = build_agent_calibration_context(
            context,
            stock_news,
            intel_events,
            agent_reports,
        )
    intel_coverage = (
        calibration_context.get('intel_coverage')
        if isinstance(calibration_context, dict)
        else None
    ) or build_intel_coverage(stock_news, intel_events)
    context['intel_coverage'] = intel_coverage
    prediction['horizon_days'] = FORCED_HORIZON_DAYS
    prediction['_intel_coverage'] = intel_coverage
    if calibration_context:
        prediction['_agent_calibration'] = calibration_context
    return calibration_context or {}


def _load_predictions_cache() -> dict:
    try:
        if _PREDICTIONS_CACHE.exists():
            return json.loads(_PREDICTIONS_CACHE.read_text(encoding='utf-8'))
    except Exception:
        pass
    return {}


def _save_predictions_cache(cache: dict):
    try:
        _PREDICTIONS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _PREDICTIONS_CACHE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def _reasoning_cache_path(code: str) -> Path:
    return _REASONING_CACHE_DIR / f'{code}.json'


def _load_reasoning_cache(code: str) -> dict | None:
    """读取推理层缓存；超过 4h 或非当日返回 None。"""
    p = _reasoning_cache_path(code)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if data.get('version') != _REASONING_CACHE_VERSION:
            return None
        if data.get('date') != date.today().strftime('%Y-%m-%d'):
            return None
        cached_at = datetime.fromisoformat(data.get('cached_at', ''))
        if (datetime.now() - cached_at).total_seconds() > _REASONING_TTL_SECONDS:
            return None
        return data
    except Exception:
        return None


def _cognition_horizon(cognition: dict | None) -> str:
    if not isinstance(cognition, dict):
        return ''
    return str(
        cognition.get('horizon')
        or cognition.get('holding_horizon')
        or cognition.get('expected_horizon')
        or ''
    ).strip()


def _profile_horizon(profile: dict | None) -> str:
    if not isinstance(profile, dict):
        return ''
    return str((profile.get('style') or {}).get('horizon') or '').strip()


def _effective_profile(profile: dict | None, cognition: dict | None) -> dict | None:
    horizon = _cognition_horizon(cognition)
    if not horizon:
        return profile
    base = dict(profile or {})
    style = dict(base.get('style') or {})
    style['horizon'] = horizon
    base['style'] = style
    return base


def _safe_float(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _safe_price(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    text = str(v).strip().replace('¥', '').replace('$', '').replace(',', '').replace('元', '')
    try:
        p = float(text)
        return p if p > 0 else None
    except ValueError:
        return None


def parse_f_layer_verdict(debate_text: str) -> tuple[str | None, int | None]:
    if not debate_text:
        return None, None
    m = re.search(r'裁决[:：]\s*(bullish|bearish|neutral)\s+置信[:：]\s*(\d+)?', debate_text)
    if not m:
        return None, None
    direction = m.group(1)
    conf = int(m.group(2)) if m.group(2) and m.group(2).isdigit() else None
    return direction, conf


def _derive_final_category(prediction: dict) -> str:
    grade = str(prediction.get('opportunity_grade') or '').strip().upper()
    direction = prediction.get('direction')
    if grade == 'D' or direction == 'bearish':
        return 'avoid'
    # F 层否决：拦截买入资格（buy/bullish_watch → watch），但不削弱已成立的 avoid
    if prediction.get('f_layer_veto'):
        return 'watch'
    if prediction.get('final_action') == 'buy':
        return 'buy'
    if grade == 'C':
        return 'watch'
    if direction == 'bullish':
        return 'bullish_watch'
    return 'watch'


def _validate_prediction_contract(prediction: dict) -> str | None:
    expected = _derive_final_category(prediction)
    actual = prediction.get('category')
    if actual == expected:
        return None
    grade = str(prediction.get('opportunity_grade') or '').strip().upper()
    return (f'category={actual} 与契约期望 {expected} 冲突'
            f'（grade={grade}/direction={prediction.get("direction")}/'
            f'action={prediction.get("final_action")}）')


def _sync_category(prediction: dict):
    # category_ai 只在首轮记录 AI 原始输出；缓存命中重放时不得用上轮派生值覆盖
    if 'category_ai' not in prediction:
        prediction['category_ai'] = prediction.get('category')
    grade = str(prediction.get('opportunity_grade') or '').strip().upper()
    direction = prediction.get('direction')
    # 安全闸门：D/bearish 强制 avoid、F 层否决把买入资格压成 watch（合法 post-G 覆盖）
    if grade == 'D' or direction == 'bearish':
        prediction['category'] = 'avoid'
    elif prediction.get('f_layer_veto') and prediction.get('category') in ('buy', 'bullish_watch'):
        prediction['category'] = 'watch'
    elif (prediction.get('category') == 'buy' and prediction.get('final_action') != 'buy'
          and prediction.get('_trade_block_reason')):
        # 系统后置降级（策略未触发/交易参数不全，由 _trade_block_reason 标记）把 buy 动作
        # 压成 watch_only：category 必须跟着回派生，否则系统自造 contract_conflict。
        # 注意：G 自身给出的 buy/watch_only 矛盾（无 _trade_block_reason）仍走下方冲突标记，不静默改写
        prediction['category'] = _derive_final_category(prediction)
    elif prediction.get('category') not in ('buy', 'avoid', 'watch', 'bullish_watch'):
        prediction['category'] = _derive_final_category(prediction)
    # 非安全闸门：保留 G 的 category，仅校验是否与契约自洽，冲突则标记不改写
    conflict = _validate_prediction_contract(prediction)
    if conflict:
        prediction['contract_conflict'] = conflict
    else:
        prediction.pop('contract_conflict', None)


def _assemble_task_blueprint(prediction: dict, trade_setup: dict | None) -> dict | None:
    setup = trade_setup if isinstance(trade_setup, dict) else {}
    category = prediction.get('category')
    if category not in ('buy', 'avoid', 'watch', 'bullish_watch'):
        prediction['_blueprint_error'] = f'category 非法: {category}'
        return None
    # 契约校验：category 必须与机会等级/方向/动作自洽，冲突即拒单，不洗白也不静默降级
    conflict = _validate_prediction_contract(prediction)
    if conflict:
        prediction['contract_conflict'] = conflict
        prediction['_blueprint_error'] = f'contract_conflict: {conflict}'
        return None
    prediction.pop('contract_conflict', None)
    entry_price = _safe_price(prediction.get('entry_price') or prediction.get('entry_ref'))
    horizon = normalize_horizon_days(prediction.get('horizon_days'))
    prediction['horizon_days'] = horizon
    confidence = _safe_float(prediction.get('confidence')) if prediction.get('confidence') is not None else 0.5
    # G 层 confidence 是 1~10 整数（1=极低），整数一律 /10；仅 (0,1) 小数视为已归一化
    if confidence > 1 or float(confidence).is_integer():
        confidence = confidence / 10
    target_pct = _safe_float(prediction.get('target_pct'))
    stop_pct = abs(_safe_float(prediction.get('stop_pct')))
    entry_trigger = _safe_price(prediction.get('entry_trigger')) or _safe_price(setup.get('entry_trigger'))
    fail_level = _safe_price(prediction.get('fail_level'))

    if not entry_price:
        prediction['_blueprint_error'] = '缺 entry_price'
        return None

    bp: dict = {
        'category': category,
        'direction': {'buy': 'bullish', 'bullish_watch': 'bullish', 'avoid': 'bearish'}.get(category, 'neutral'),
        'entry_price': round(entry_price, 3),
        'confidence': round(min(1.0, max(0.0, confidence)), 2),
        'horizon_days': horizon,
        'strategy_pattern': str(setup.get('setup_name') or ''),
        'f_layer_veto': bool(prediction.get('f_layer_veto')),
    }
    for key in ('strategy_family', 'strategy_name', 'strategy_stage', 'strategy_reason'):
        if prediction.get(key):
            bp[key] = prediction.get(key)
    if category == 'buy':
        setup_strategy_map = {
            'trend_breakout': '放量平台/趋势突破',
            'box_breakout': '放量平台/趋势突破',
            'pullback_buy': '强趋势缩量回踩低吸',
            'oversold_rebound': '超跌反弹确认',
            'event_momentum': '事件催化观察',
            'downtrend_avoid': '下跌回避/失败结构',
            'trend_continuation': '强趋势延续/主线龙头跟随',
        }
        buy_strategy = (
            prediction.get('buy_strategy')
            or prediction.get('strategy_name')
            or setup_strategy_map.get(str(setup.get('setup_name') or ''))
        )
        if buy_strategy:
            bp['buy_strategy'] = buy_strategy
        elif isinstance(prediction.get('strategy_profile'), dict) and prediction.get('strategy_profile'):
            prediction['_blueprint_error'] = 'buy 缺少 buy_strategy'
            return None
    if category == 'buy':
        if not (target_pct > 0 and stop_pct > 0):
            prediction['_blueprint_error'] = 'buy 缺 target/stop'
            return None
        bp['target_pct'] = round(target_pct, 2)
        bp['stop_pct'] = round(stop_pct, 2)
    elif category == 'avoid':
        # 失效价缺失或方向错误（G 给了现价下方的支撑位，如宁德）时，用止损幅度兜底出现价上方的失效价
        if (not fail_level or fail_level <= entry_price) and stop_pct > 0 and entry_price:
            fail_level = round(entry_price * (1 + stop_pct / 100), 3)
            prediction['fail_level'] = fail_level
        if not (target_pct > 0 and fail_level):
            prediction['_blueprint_error'] = 'avoid 缺预测跌幅/失效价'
            return None
        if fail_level <= entry_price:
            prediction['_blueprint_error'] = 'avoid 失效价须高于当前价'
            return None
        bp['target_pct'] = round(target_pct, 2)
        bp['fail_level'] = round(fail_level, 3)
    elif category == 'watch':
        rng = prediction.get('expected_range_pct')
        try:
            lo, hi = float(rng[0]), float(rng[1])
        except (TypeError, ValueError, IndexError):
            prediction['_blueprint_error'] = 'watch 缺 expected_range'
            return None
        if lo >= hi:
            prediction['_blueprint_error'] = 'watch expected_range 上下颠倒'
            return None
        bp['expected_range'] = [round(lo, 2), round(hi, 2)]
    else:
        # 仅 bullish_watch 允许回退 trade_setup 的 fail_level（下方支撑）；
        # avoid 的失效价语义是上方阻力，回退支撑位必然反向，禁止回退
        fail_level = fail_level or _safe_price(setup.get('fail_level'))
        if not (entry_trigger and fail_level and target_pct > 0):
            prediction['_blueprint_error'] = 'bullish_watch 缺触发价/失效价/触发后目标'
            return None
        # 个股已运行到日线触发价上方时，触发价改用现价口径，避免"等站上一个已被站上的陈旧触发价"
        if entry_price and entry_trigger < entry_price:
            entry_trigger = entry_price
        if fail_level >= entry_trigger:
            prediction['_blueprint_error'] = 'bullish_watch 失效价须低于触发价'
            return None
        bp['entry_trigger'] = round(entry_trigger, 3)
        bp['fail_level'] = round(fail_level, 3)
        bp['triggered_target_pct'] = round(target_pct, 2)
    prediction.pop('_blueprint_error', None)
    return bp


def _build_holding_advice(prediction: dict, cognition: dict | None) -> str | None:
    if not isinstance(cognition, dict):
        return None

    if 'has_position' in cognition:
        has_position = bool(cognition.get('has_position'))
    else:
        has_position = bool(
            cognition.get('is_holding')
            or cognition.get('holding')
            or _safe_float(cognition.get('shares')) > 0
            or _safe_float(cognition.get('cost_price')) > 0
        )
    direction = str(prediction.get('direction') or 'neutral')
    current_price = _safe_float(prediction.get('entry_price') or prediction.get('entry_ref'))
    cost_price = _safe_float(cognition.get('cost_price') or cognition.get('cost'))
    holding_direction = str(cognition.get('direction') or 'bullish')

    parts: list[str] = []
    if not has_position:
        if direction == 'bullish':
            parts.append('当前无持仓，可关注入场参考价位')
        else:
            return None
    elif direction == 'neutral':
        parts.append('当前预测偏中性，持仓以风控和仓位管理为主')
    elif holding_direction == direction or (holding_direction in ('long', 'bullish', '多') and direction == 'bullish'):
        parts.append('当前持仓方向与预测一致，可继续持有')
    else:
        parts.append('预测方向与持仓相反，关注止损位')

    if cost_price > 0 and current_price > 0:
        pnl_pct = (current_price - cost_price) / cost_price * 100
        if pnl_pct > 0:
            parts.append(f'当前相对成本价浮盈约 {pnl_pct:.1f}%，可考虑上移止损保护利润')
        elif pnl_pct < 0:
            parts.append(f'当前相对成本价浮亏约 {abs(pnl_pct):.1f}%，需严格关注止损位')
        else:
            parts.append('当前接近成本价，重点观察方向确认信号')

    cog_horizon = _cognition_horizon(cognition)
    prof_horizon = str(cognition.get('_profile_horizon') or '').strip()
    if cog_horizon and prof_horizon and cog_horizon != prof_horizon:
        parts.append(f'时间维度冲突：本次填写期限为「{cog_horizon}」，用户画像偏好为「{prof_horizon}」，本次以填写期限优先')

    return '；'.join(parts) if parts else None


def _extract_agent4_levels(tech_report, current_price: float | None) -> tuple[float, float]:
    """从 Agent4 技术报告中提取目标/止损百分比，失败返回保守默认值 (5.0, 3.0)。"""
    import re
    default = (5.0, 3.0)
    if not isinstance(tech_report, dict) or not current_price or current_price <= 0:
        return default
    summary = tech_report.get('summary', '') or ''
    evidence = tech_report.get('evidence') or []
    text = summary + ' '.join(e.get('value', '') for e in evidence if isinstance(e, dict))
    targets = re.findall(r'目标[参考价位：:]*\s*([\d.]+)', text)
    stops = re.findall(r'止损[参考价位：:]*\s*([\d.]+)', text)
    try:
        if targets:
            t_price = float(targets[0])
            t_pct = abs(t_price - current_price) / current_price * 100
            if 0.5 < t_pct < 30:
                target = round(t_pct, 1)
            else:
                target = default[0]
        else:
            target = default[0]
        if stops:
            s_price = float(stops[0])
            s_pct = abs(s_price - current_price) / current_price * 100
            if 0.5 < s_pct < 20:
                stop = round(s_pct, 1)
            else:
                stop = default[1]
        else:
            stop = default[1]
        return (target, stop)
    except Exception:
        return default


def _enforce_tradeability(prediction: dict):
    direction = prediction.get('direction')
    if direction not in ('bullish', 'bearish'):
        return
    action = prediction.get('final_action')
    missing = []
    missing_entry = not prediction.get('entry_ref') and action == 'buy'
    if missing_entry:
        missing.append('入场参考')
    if prediction.get('target_pct') is None:
        missing.append('目标')
    if prediction.get('stop_pct') is None:
        missing.append('止损')
    if not prediction.get('horizon_days'):
        missing.append('周期')
    if not missing:
        return
    prediction['final_action'] = 'watch_only'
    prediction['final_rating'] = 'hold'
    prediction['entry_ref'] = None
    if missing_entry:
        prediction['entry_price'] = 0
    prediction['_trade_block_reason'] = '交易参数不完整：' + '/'.join(missing)


def _derive_decision_view(
    prediction: dict,
    trade_setup: dict,
    opportunity_profile: dict | None,
    context: dict | None = None,
) -> dict:
    """生成非破坏性 decision_view：补充方向倾向、动作说明和验证口径。

    优先级：确定性机会分层 > direction/final_action > market_phase/behavior_tags/资金风险。
    不改变买卖动作，只补充真实展示与复盘口径。
    """
    context = context or {}
    opportunity_profile = opportunity_profile or {}
    grade = opportunity_profile.get('opportunity_grade') or ''
    phase = opportunity_profile.get('setup_phase') or ''
    direction = prediction.get('direction', 'neutral')
    final_action = prediction.get('final_action', 'hold')
    market_phase = context.get('market_phase', '')
    profile = context.get('technical_profile') or {}
    tags = profile.get('behavior_tags') or []
    flow = context.get('flow_profile') or {}
    reason_tags: list[str] = []

    # ── 主映射：机会等级 → bias_label / readiness / evaluation_scope ──
    if grade == 'S':
        if '高风险' in str(phase):
            bias_label = '风险看多'
            readiness = 'risk_triggered'
            evaluation_scope = 'trade'
            reason_tags.append('S 高风险触发')
        else:
            bias_label = '看多'
            readiness = 'triggered'
            evaluation_scope = 'trade'
            reason_tags.append('S 已触发')
    elif grade == 'A':
        bias_label = '临界看多'
        readiness = 'near_trigger'
        evaluation_scope = 'direction_watch'
        reason_tags.append('A 临界机会')
    elif grade == 'B':
        bias_label = '看多观察'
        readiness = 'candidate'
        evaluation_scope = 'direction_watch'
        reason_tags.append('B 持仓观察')
    elif grade == 'D':
        bias_label = '看空回避'
        readiness = 'avoid'
        evaluation_scope = 'avoid_watch'
        reason_tags.append('D 回避')
    elif grade == 'C':
        bias_label = '中性震荡'
        readiness = 'weak'
        evaluation_scope = 'neutral_watch'
        reason_tags.append('C 弱观察')
    else:
        # 无机会分层时，用 direction + final_action 推导
        if direction == 'bullish':
            if final_action == 'buy':
                bias_label = '看多'
                readiness = 'triggered'
                evaluation_scope = 'trade'
            elif final_action in ('watch_only', 'hold'):
                bias_label = '看多观察'
                readiness = 'candidate'
                evaluation_scope = 'direction_watch'
                reason_tags.append('方向偏多未触发')
            else:
                bias_label = '中性震荡'
                readiness = 'weak'
                evaluation_scope = 'neutral_watch'
        elif direction == 'bearish':
            bias_label = '看空回避'
            readiness = 'avoid'
            evaluation_scope = 'avoid_watch'
        else:
            bias_label = '中性震荡'
            readiness = 'weak'
            evaluation_scope = 'neutral_watch'

    # ── 次因调整：市场环境 / 行为标签 / 资金 ──
    if market_phase in ('ice', 'decline', 'retreat'):
        reason_tags.append('退潮/冰点环境')
        if bias_label in ('看多观察', '临界看多') and readiness != 'triggered':
            bias_label = '中性震荡'
            readiness = 'weak'
            evaluation_scope = 'neutral_watch'

    tag_names = [str(item.get('tag') or '') for item in tags if isinstance(item, dict)]
    if any(name in tag_names for name in ('数据不足', '数据异常')):
        reason_tags.append('技术数据不足')
        if readiness not in ('triggered', 'risk_triggered', 'avoid'):
            bias_label = '中性震荡'
            readiness = 'weak'
            evaluation_scope = 'neutral_watch'

    if any(name in tag_names for name in ('疑似启动初段', '疑似底部背离', '疑似底部放量')):
        if readiness == 'candidate':
            reason_tags.append('正向技术行为信号')

    # 资金面调整
    if isinstance(flow, dict) and flow.get('available'):
        main_5d = flow.get('main_5d', 0) or 0
        small_5d = flow.get('small_5d', 0) or 0
        if main_5d < 0 and small_5d > 0:
            reason_tags.append('主力流出散户接盘')
            if readiness in ('near_trigger', 'candidate'):
                bias_label = '中性震荡'
                readiness = 'weak'
                evaluation_scope = 'neutral_watch'
        elif main_5d > 0:
            reason_tags.append('主力资金流入')

    # ── F 层否决权：bearish 且置信≥6 时压档；S 触发保结构、撤买入资格 ──
    f_veto = bool(prediction.get('f_layer_veto'))
    f_dir = prediction.get('f_layer_direction')
    f_conf = prediction.get('f_layer_confidence')
    if f_veto:
        bias_label = '看空回避'
        evaluation_scope = 'avoid_watch'
        readiness = 'risk_vetoed' if grade == 'S' else 'avoid'

    # ── 对齐闸门：category 是评分口径真相，bias_label/readiness/scope 必须服从 ──
    bias_label, readiness, evaluation_scope = reconcile_view_to_category(
        prediction.get('category'), bias_label, readiness, evaluation_scope, grade,
        f_layer_veto=f_veto)

    # ── 动作说明 ──
    if final_action == 'buy':
        action_label = '可轻仓试错，严格止损'
    elif final_action in ('watch_only', 'hold'):
        if readiness in ('near_trigger', 'candidate'):
            action_label = '偏多但未触发，等待确认后买入'
        elif readiness == 'triggered':
            action_label = '已触发，按失效价持有'
        elif readiness == 'risk_triggered':
            action_label = '不重仓追高，轻仓试错或等待回踩'
        elif readiness == 'avoid':
            action_label = '不买，等待技术修复'
        else:
            action_label = '只观察不参与'
    elif final_action in ('sell', 'reduce'):
        action_label = '减仓或退出，控制风险'
    elif final_action == 'not_suitable':
        action_label = '不适合当前用户'
    else:
        action_label = '观望'

    # ── 融合标签（仅 UI，不替代 bias_label 机器职责）──
    if grade == 'S':
        structure_word = '已触发'
    elif grade in ('A', 'B'):
        structure_word = '偏多'
    elif grade == 'D':
        structure_word = '偏空'
    else:
        structure_word = '中性'
    f_word = {'bullish': '看多', 'bearish': '看空', 'neutral': '中性'}.get(f_dir, '中性')
    if f_veto:
        fusion_label = f'结构{structure_word} · F层{f_word}({f_conf or "?"}/10) · 利空否决，只观察不参与'
    else:
        fusion_label = f'结构{structure_word} · F层{f_word}({f_conf or "?"}/10) · {action_label}'

    return {
        'bias_label': bias_label,
        'action_label': action_label,
        'readiness': readiness,
        'evaluation_scope': evaluation_scope,
        'reason_tags': list(dict.fromkeys(reason_tags)),
        'fusion_label': fusion_label,
    }


def _has_position(cognition: dict | None) -> bool:
    if not isinstance(cognition, dict):
        return False
    if 'has_position' in cognition:
        return bool(cognition.get('has_position'))
    return bool(
        cognition.get('is_holding')
        or cognition.get('holding')
        or _safe_float(cognition.get('shares')) > 0
        or _safe_float(cognition.get('cost_price')) > 0
    )


def _position_hint_by_phase(status: str | None, market_phase: str | None) -> str:
    """情绪周期×状态 → 仓位建议（北京炒家纪律：只调仓位，不碰 confidence/direction）。"""
    triggered = status == 'triggered'
    if market_phase in ('decline', 'ice'):
        return '空仓观望（情绪冰点/衰退）' if triggered else '禁止新开仓（情绪冰点/衰退）'
    if market_phase == 'main_up':
        return '半仓，禁满仓追高' if triggered else '轻仓试错'
    if market_phase == 'repair':
        return '1/3 试仓，次日放量确认再加' if triggered else '观察，不触发不买'
    if market_phase == 'retreat':
        return '轻仓，只减不加' if triggered else '不开新仓，等待企稳'
    # market_phase 未知：回退原有 status 粗分
    if triggered:
        return '轻仓试错，跌破失效价退出'
    if status == 'candidate':
        return '观察，不触发不买'
    return '禁止新开仓，已有仓位优先风控'


def derive_trade_setup(context: dict) -> dict:
    """确定性短线交易模式：候选不等于买点。"""
    setup_label = {
        'trend_breakout': '趋势突破',
        'box_breakout': '箱体突破',
        'pullback_buy': '上升回踩',
        'oversold_rebound': '超跌反弹',
        'event_momentum': '事件催化',
        'downtrend_avoid': '下跌回避',
        'trend_continuation': '强趋势延续',
    }
    trend_label = {
        'uptrend': '上升趋势',
        'downtrend': '下降趋势',
        'recovery': '修复趋势',
        'sideways': '震荡',
    }
    tech = context.get('technical_profile') or {}
    flow = context.get('flow_profile') or {}
    if not tech.get('available'):
        return {
            'setup_name': 'downtrend_avoid',
            'status': 'avoid',
            'entry_trigger': None,
            'fail_level': None,
            'target_level': None,
            'suggested_horizon_days': FORCED_HORIZON_DAYS,
            'position_hint': '数据不足，不开新仓',
            'reason': '技术画像不可用',
            'risk_flags': ['技术数据不足'],
        }

    setup_name = tech.get('setup_name') or 'event_momentum'
    secondary_setups = [
        str(x) for x in (tech.get('secondary_setups') or [])
        if str(x) and str(x) != setup_name
    ]
    setup_candidates = [
        x for x in (tech.get('setup_candidates') or [])
        if isinstance(x, dict)
    ]
    status = tech.get('status') or 'candidate'
    risk_flags = list(tech.get('risk_flags') or [])
    reason = f"{trend_label.get(tech.get('trend_stage'), '趋势不明')} / {setup_label.get(setup_name, setup_name)}"

    if flow.get('available'):
        if flow.get('main_5d', 0) < 0 and flow.get('small_5d', 0) > 0:
            risk_flags.append('近5日主力流出且散户接盘')
        if flow.get('main_5d', 0) < 0 and status == 'triggered':
            status = 'candidate'
            reason += '；突破受资金流拖累，降级为候选'

    if setup_name in ('downtrend_avoid',) or '跌破20日平台低点' in risk_flags:
        status = 'avoid'

    market_phase = context.get('market_phase') or 'unknown'
    position_hint = _position_hint_by_phase(status, market_phase)

    return {
        'setup_name': setup_name,
        'secondary_setups': secondary_setups,
        'setup_candidates': setup_candidates,
        'status': status,
        'entry_trigger': tech.get('entry_trigger'),
        'fail_level': tech.get('fail_level'),
        'target_level': tech.get('target_level'),
        'suggested_horizon_days': FORCED_HORIZON_DAYS,
        'position_hint': position_hint,
        'reason': reason,
        'risk_flags': risk_flags,
    }


def derive_opportunity_profile(context: dict, trade_setup: dict) -> dict:
    """确定性机会分层：把“是否有机会”和“现在能不能买”拆开。"""
    context = context or {}
    trade_setup = trade_setup or {}
    tech = context.get('technical_profile') or {}
    flow = context.get('flow_profile') or {}

    def _nested(src: dict, key: str, subkey: str):
        val = src.get(key)
        if isinstance(val, dict):
            return val.get(subkey)
        return None

    def _num(*vals):
        for val in vals:
            n = _safe_float(val)
            if n:
                return n
        return 0.0

    status = trade_setup.get('status') or tech.get('status') or 'candidate'
    setup_name = trade_setup.get('setup_name') or tech.get('setup_name') or 'event_momentum'
    secondary_setups = [
        str(x) for x in (trade_setup.get('secondary_setups') or tech.get('secondary_setups') or [])
        if str(x) and str(x) != setup_name
    ]
    is_dipbuy = setup_name == 'pullback_buy'
    is_trend_follow = setup_name == 'trend_continuation'
    _mf = context.get('mainline_fit') if isinstance(context.get('mainline_fit'), dict) else {}
    major_mainline = bool(_mf.get('major_mainline'))
    rs_lead = bool(_mf.get('rs_lead'))
    is_etf_flag = bool(_mf.get('is_etf'))
    entry = _num(trade_setup.get('entry_trigger'), tech.get('entry_trigger'))
    fail = _num(trade_setup.get('fail_level'), tech.get('fail_level'))
    target = _num(trade_setup.get('target_level'), tech.get('target_level'))
    close = _num(context.get('_kline_last_close'), tech.get('close'), context.get('current_price'), entry)
    volume_ratio = _num(tech.get('volume_ratio'), _nested(tech, 'volume', 'volume_ratio'))
    turnover = _num(tech.get('turnover'), _nested(tech, 'volume', 'turnover'))
    rsi = _num(tech.get('rsi14'), _nested(tech, 'indicators', 'rsi14'), _nested(tech, 'momentum', 'rsi14'))
    kdj_j = _num(tech.get('kdj_j'), _nested(tech, 'indicators', 'kdj_j'), _nested(tech, 'momentum', 'kdj_j'))
    ma20_slope = _num(_nested(tech, 'ma', 'ma20_slope_pct'), tech.get('ma20_slope_pct'))
    ma5 = _num(_nested(tech, 'ma', 'ma5'), tech.get('ma5'))

    risk_flags = list(trade_setup.get('risk_flags') or [])
    evidence = []
    gaps = []

    if entry and close:
        trigger_distance_pct = round((entry - close) / close * 100, 2)
    else:
        trigger_distance_pct = None
    risk_reward = None
    if close and fail and target and close > fail:
        risk_reward = round((target - close) / (close - fail), 2)
    elif entry and fail and target and entry > fail:
        risk_reward = round((target - entry) / (entry - fail), 2)

    flow_days = int(_safe_float(flow.get('days')) or 0) if isinstance(flow, dict) else 0
    main_flow = _num(flow.get('main_5d'), flow.get('main_3d'), flow.get('main_net'))
    small_flow = _num(flow.get('small_5d'), flow.get('small_3d'), flow.get('small_net'))
    flow_positive = main_flow > 0 and small_flow <= 0
    flow_negative = main_flow < 0 and small_flow > 0
    flow_confirmed = flow_positive and flow_days >= 3

    if flow_confirmed:
        evidence.append('主力流入、散户流出，资金结构偏正面（≥3日延续）')
    if flow_negative:
        risk_flags.append('主力流出且散户接盘')
    if flow_days and flow_days < 3:
        gaps.append(f'资金仅{flow_days}日数据，连续性不足')

    volume_ok = volume_ratio >= 1.2
    volume_weak = volume_ratio > 0 and volume_ratio < 1.0
    if volume_ok:
        evidence.append(f'量比{volume_ratio}，达到放量确认线')
    elif volume_weak and is_dipbuy:
        evidence.append(f'量比{volume_ratio}，缩量回踩（低吸健康，非缺量）')
    elif volume_weak and is_trend_follow:
        evidence.append(f'量比{volume_ratio}，缩量延续（强趋势健康缩量，非缺量）')
    elif volume_weak:
        gaps.append(f'量比{volume_ratio}，临界或突破缺少放量')

    if is_dipbuy or is_trend_follow:
        # 低吸回踩 / 强趋势延续：缩量为健康信号，不套放量门槛，沿用趋势判据
        structure_ok = bool(tech.get('trend_stage') in ('uptrend', 'recovery') or ma20_slope > 0)
    else:
        # 突破/动量：短周期结构在=站上MA5且量比≥1.2；MA20斜率仅作辅助证据
        structure_ok = bool(ma5 and close and close >= ma5 and volume_ratio >= 1.2)
    uptrend = structure_ok
    if ma20_slope > 0:
        evidence.append('中期均线辅助向上')
    if uptrend:
        evidence.append('短线结构在位')

    overheat = (rsi >= 80) or (kdj_j >= 90) or (turnover >= 20)
    if overheat:
        risk_flags.append('短线过热或高换手，禁止重仓追高')
    elif rsi >= 70 or kdj_j >= 80:
        risk_flags.append('短线超买，追高需等待确认')
    # 真见顶信号（放量长上影/放量阴线/天量换手）≠ 单纯超买；强趋势龙头只受见顶信号约束，
    # 不被 RSI/KDJ 超买这类死指标一刀切，符合"政策主线天天新高"龙头的现实
    topping = ('长上影冲高回落' in risk_flags) or ('放量阴线' in risk_flags) or (turnover >= 25)

    liquidity_block = (volume_ratio > 0 and volume_ratio < 0.5 and not is_dipbuy) or flow_negative
    if volume_ratio > 0 and volume_ratio < 0.5 and not is_dipbuy:
        risk_flags.append('量比极低，流动性枯竭/信号不可验证')

    if not tech.get('available') or status in ('avoid', 'failed') or setup_name == 'downtrend_avoid':
        grade, label, phase = 'D', 'D 回避', '回避'
        not_holding_plan = '不买，等待技术画像恢复或重新站回关键位后再评估'
        holding_plan = '已有仓位只做风控，反弹不加仓，跌破防守条件减仓或退出'
    elif risk_reward is not None and risk_reward < 1.5:
        grade, label, phase = 'D', 'D 回避', '风险收益比不合格'
        not_holding_plan = '不买，当前目标空间不足以覆盖失效风险'
        holding_plan = '已有仓位降低预期，按防守位处理'
        risk_flags.append(f'风险收益比{risk_reward}低于1.5')
    elif is_trend_follow and status == 'triggered' and not topping and (
        main_flow > 0
        or (is_etf_flag and (_mf.get('sector_strong') and rs_lead))
        or (not is_etf_flag and major_mainline and rs_lead)
    ):
        # 强趋势延续视为已触发买点（缩量/超买健康，不要求放量），仅真见顶信号才不给 S。
        # 资金确认：一般需主力净流入；大主线(AI链)龙头可用 RS 领先大盘替代资金确认（资金/RS 二选一）
        phase = '已触发' if flow_confirmed else '高风险触发'
        label = 'S 已触发机会' if flow_confirmed else 'S 已触发但高风险'
        grade = 'S'
        if is_etf_flag:
            if flow_confirmed:
                not_holding_plan = '板块趋势延续且资金净流入，可按现价轻仓跟随ETF，严格执行失效价'
                holding_plan = '持有，缩量延续不必离场；跌破失效价或板块转弱则退出'
            else:
                not_holding_plan = '板块趋势延续，仅轻仓跟随ETF，资金连续性未确认，跌破失效价立即退出'
                holding_plan = '持有但上移止损，板块转弱立即退出'
        elif flow_confirmed:
            not_holding_plan = '主线强趋势延续且主力净流入，可按现价轻仓跟随，严格执行失效价'
            holding_plan = '持有，缩量延续不必离场；跌破失效价或主力转为持续流出则退出'
        else:
            not_holding_plan = '主线强趋势延续，仅轻仓跟随，主力连续性未确认，跌破失效价立即退出'
            holding_plan = '持有但上移止损，主力转为流出立即退出'
    elif status == 'triggered' and volume_ratio >= 1.5:
        if overheat:
            grade, label, phase = 'B', 'B 持仓观察', '过热未触发'
            not_holding_plan = '短线过热/高换手，不追高，等待回踩确认'
            holding_plan = '已有仓位上移止损，不加仓'
        else:
            phase = '高风险触发' if not flow_confirmed else '已触发'
            label = 'S 已触发但高风险' if phase == '高风险触发' else 'S 已触发机会'
            grade = 'S'
            if phase == '高风险触发':
                not_holding_plan = '不重仓追高；只允许轻仓试错，或等待回踩后重新确认'
                holding_plan = '已有仓位继续持有，但上移止损，跌破失效价立即退出'
            else:
                not_holding_plan = '可按触发价轻仓试错，严格执行失效价'
                holding_plan = '持有；只有放量延续且不跌回触发价才考虑加仓'
    elif status == 'triggered':
        grade, label, phase = 'B', 'B 持仓观察', '触发缺量降级'
        not_holding_plan = '突破未放量(<1.5)，等待量能确认再参与'
        holding_plan = '已有仓位可守，缺量不加仓'
    elif status == 'candidate' and uptrend and entry and trigger_distance_pct is not None and abs(trigger_distance_pct) <= 1.0 and volume_ratio >= 1.5 and not flow_negative:
        grade, label, phase = 'A', 'A 临界机会', '临界未触发'
        not_holding_plan = f'等待放量站上{entry}后再买，未确认前不追'
        holding_plan = '已有仓位可持有观察，但不加仓'
    elif uptrend and status == 'candidate':
        grade, label, phase = 'B', 'B 持仓观察', '候选未触发'
        not_holding_plan = '未持仓不追，等待触发价、量能和资金连续性同时改善'
        holding_plan = '已有仓位可守，跌破防守位或资金继续流出则减仓'
    else:
        grade, label, phase = 'C', 'C 弱观察', '弱候选'
        not_holding_plan = '只观察，不提前埋伏'
        holding_plan = '已有仓位降低仓位弹性，优先保护本金'

    if liquidity_block and grade in ('S', 'A', 'B'):
        grade = 'C'
        label, phase = 'C 弱观察', '流动性闸门压档'
        not_holding_plan = '量比极低或主力流出散户接盘，信号不可验证，不参与'
        holding_plan = '已有仓位按防守位处理，不加仓'

    if grade in ('C', 'D'):
        evidence = [e for e in evidence if '短线结构在位' not in e]

    if grade == 'D':
        attack_level = '回避，不参与；等待技术修复并重新站回关键位后再重新验证'
    elif grade == 'C':
        attack_level = '弱观察，不提前埋伏；等待放量与资金/结构重新确认后再评估'
    elif is_dipbuy:
        attack_level = (f'回踩企稳低吸（{entry}附近），不破{fail}持有；收复MA5或次日放量站回加确认'
                        if fail else '回踩缩量企稳低吸，放量阴线/破位则失效')
    elif is_trend_follow:
        attack_level = (f'强趋势延续轻仓跟随（站稳MA5，{entry}上方），不破{fail}持有；缩量上涨健康，无需等待放量'
                        if fail else '强趋势延续轻仓跟随，跌破MA5或放量长上影则失效')
    else:
        attack_level = f'放量站上{entry}' if entry else '等待明确触发价'
        if entry and volume_ratio < 1.2:
            attack_level = f'放量站上{entry}，量比>1.2'
    defense_level = f'跌破{fail}' if fail else '技术数据恢复后重评'
    if flow_negative:
        defense_level += '，或主力资金继续流出'

    position_hint = {
        'S': '轻仓试错' if phase == '高风险触发' else '可试错',
        'A': '观察，不触发不买',
        'B': '持仓观察，不追高',
        'C': '弱观察',
        'D': '回避',
    }.get(grade, '观察')

    return {
        'opportunity_grade': grade,
        'opportunity_label': label,
        'setup_phase': phase,
        'setup_name': setup_name,
        'secondary_setups': secondary_setups,
        'status': status,
        'trigger_distance_pct': trigger_distance_pct,
        'risk_reward': risk_reward,
        'attack_level': attack_level,
        'defense_level': defense_level,
        'not_holding_plan': not_holding_plan,
        'holding_plan': holding_plan,
        'add_condition': f'{attack_level}，且资金连续流入' if grade in ('A', 'B', 'S') else '无加仓条件',
        'reduce_condition': defense_level,
        'exit_condition': defense_level,
        'position_hint': position_hint,
        'evidence': evidence[:5],
        'risk_flags': list(dict.fromkeys([str(x) for x in risk_flags if x]))[:6],
        'evidence_gaps': gaps[:5],
    }


def _apply_etf_final_cap(prediction: dict, is_etf_code: bool, current_price):
    if not is_etf_code or not isinstance(prediction, dict):
        return
    if prediction.get('direction') != 'bullish' or prediction.get('final_action') == 'not_suitable':
        return
    t = _safe_float(prediction.get('target_pct'))
    s = _safe_float(prediction.get('stop_pct'))
    if t > 0:
        prediction['target_pct'] = round(min(t, 8.0), 1)
    if s > 0:
        prediction['stop_pct'] = round(min(s, 5.0), 1)
    _t = _safe_float(prediction.get('target_pct'))
    _s = _safe_float(prediction.get('stop_pct'))
    if _t > 0 and _s > 0 and _t < _s * 2.0:
        if prediction.get('final_action') == 'buy':
            prediction['final_action'] = 'watch_only'
            prediction['_trade_block_reason'] = 'ETF目标≤8%/止损≤5%夹逼后盈亏比<2:1'
            _sync_category(prediction)


def _apply_trade_setup_constraints(
    prediction: dict,
    trade_setup: dict,
    cognition: dict | None,
    opportunity_profile: dict | None = None,
    market_phase: str | None = None,
    dipbuy_evidence: dict | None = None,
):
    """把短线交易纪律落成硬约束，避免候选形态被当成买点。"""
    if not isinstance(prediction, dict) or not isinstance(trade_setup, dict):
        return
    prediction['_trade_setup'] = trade_setup
    # 落盘 setup_type（供 tracking performance_by_setup 分组）+ 提升情绪仓位到顶层；
    # 本函数 fresh/cache 两路都会执行，覆盖推理缓存路径。
    prediction['setup_type'] = trade_setup.get('setup_name')
    secondary_setups = [
        str(x) for x in (trade_setup.get('secondary_setups') or [])
        if str(x) and str(x) != str(trade_setup.get('setup_name') or '')
    ]
    prediction['secondary_setups'] = secondary_setups
    prediction['setup_types'] = [
        x for x in [trade_setup.get('setup_name'), *secondary_setups]
        if x
    ]
    if trade_setup.get('position_hint'):
        prediction['position_hint'] = trade_setup.get('position_hint')
    if isinstance(opportunity_profile, dict):
        prediction['_opportunity_profile'] = opportunity_profile
        for key in (
            'opportunity_grade', 'opportunity_label', 'setup_phase',
            'not_holding_plan', 'holding_plan', 'add_condition',
            'reduce_condition', 'exit_condition', 'risk_reward',
            'attack_level', 'defense_level', 'evidence_gaps',
        ):
            if key in opportunity_profile:
                prediction[key] = opportunity_profile.get(key)
    # 对外呈现的 trade_setup：bearish 不挂多头目标/盈亏比（trade_setup 是多头结构，与看空 thesis 矛盾）；
    # 并把建议周期对齐最终 horizon_days，消除 suggested_horizon_days 与 horizon_days 两套口径
    ts_view = dict(trade_setup)
    _hz = prediction.get('horizon_days')
    if _hz:
        ts_view['suggested_horizon_days'] = int(_hz)
    if prediction.get('direction') == 'bearish':
        ts_view['target_level'] = None
        prediction['risk_reward'] = None
        if isinstance(prediction.get('_opportunity_profile'), dict):
            prediction['_opportunity_profile']['risk_reward'] = None
    prediction['_trade_setup'] = ts_view
    status = trade_setup.get('status')
    has_pos = _has_position(cognition)
    setup_cn = {
        'trend_breakout': '趋势突破',
        'box_breakout': '箱体突破',
        'pullback_buy': '上升回踩',
        'oversold_rebound': '超跌反弹',
        'event_momentum': '事件催化',
        'downtrend_avoid': '下跌回避',
    }.get(trade_setup.get('setup_name'), trade_setup.get('setup_name') or status)

    if (not has_pos) and prediction.get('direction') == 'bearish':
        prediction['final_action'] = 'watch_only'
        prediction['final_rating'] = 'hold'
        prediction['entry_ref'] = None
        prediction['_trade_block_reason'] = '无持仓看空=不买/等待，不显示卖出'

    if has_pos and prediction.get('direction') == 'bearish':
        if prediction.get('final_action') not in ('sell', 'reduce'):
            prediction['final_action'] = 'reduce'
            prediction['final_rating'] = 'reduce'
            prediction.setdefault('_trade_block_reason', '看空方向+已有持仓：建议减仓')

    grade = (opportunity_profile or {}).get('opportunity_grade')
    if grade in ('C', 'D') and prediction.get('final_action') == 'buy':
        prediction['final_action'] = 'watch_only'
        prediction['final_rating'] = 'hold'
        prediction['entry_ref'] = None
        prediction['_trade_block_reason'] = (opportunity_profile or {}).get(
            'not_holding_plan',
            '机会评级C/D，不满足买入条件，等待触发和验证'
        )
    if grade == 'D':
        prediction['direction'] = 'bearish' if prediction.get('direction') == 'bullish' else prediction.get('direction', 'neutral')
        prediction['entry_ref'] = None
        prediction.setdefault('_trade_block_reason', '机会分层为D回避，不允许新增买入')
    if prediction.get('final_action') == 'buy':
        rr = (opportunity_profile or {}).get('risk_reward')
        if rr is None or rr < 2.0:
            prediction['final_action'] = 'watch_only'
            prediction['final_rating'] = 'hold'
            prediction['entry_ref'] = None
            prediction['_trade_block_reason'] = (
                '风险收益比缺失，不允许买入' if rr is None
                else '风险收益比低于2.0，不允许买入'
            )
    if grade == 'S' and (opportunity_profile or {}).get('setup_phase') == '高风险触发':
        prediction.setdefault(
            '_trade_block_reason',
            (opportunity_profile or {}).get('not_holding_plan') or '已触发但高风险，不得重仓追高'
        )
    _apply_dipbuy_evidence_gate(prediction, trade_setup, market_phase, dipbuy_evidence)

    if status in ('candidate', 'avoid', 'failed'):
        if prediction.get('final_action') == 'buy' or prediction.get('direction') == 'bullish':
            prediction['final_action'] = 'watch_only'
            prediction['final_rating'] = 'hold'
            prediction['entry_ref'] = None
            prediction['_trade_block_reason'] = (
                '短线模式未触发：'
                f"{setup_cn} / 候选未触发，"
                f"触发价 {trade_setup.get('entry_trigger') or '—'}"
            )
        elif status == 'candidate':
            prediction.setdefault(
                '_trade_block_reason',
                f"短线模式未触发：{setup_cn} / 候选未触发，触发价 {trade_setup.get('entry_trigger') or '—'}"
            )
    if status == 'avoid' and not has_pos:
        prediction['final_action'] = 'watch_only'
        prediction['final_rating'] = 'hold'
        prediction['entry_ref'] = None
    if status in ('avoid', 'failed'):
        prediction.setdefault(
            '_trade_block_reason',
            f"短线模式禁止买入：{trade_setup.get('reason') or trade_setup.get('setup_name') or status}"
        )
    if has_pos and prediction.get('final_action') == 'buy':
        prediction['final_action'] = 'hold'
        prediction['final_rating'] = 'hold'
        prediction.setdefault(
            '_trade_block_reason',
            '已有持仓：不提示新增买入，按触发价/失效价做持仓风控'
        )
    if has_pos and prediction.get('final_action') == 'watch_only':
        prediction['final_action'] = 'hold'
        prediction['final_rating'] = 'hold'
        prediction.setdefault(
            '_trade_block_reason',
            '已有持仓：候选未触发时不加仓，按失效价做持仓风控'
        )


_INDEX_RET_CACHE: dict = {'ts': 0.0, 'ret20': None}


def _index_ret20(window: int = 20) -> float | None:
    """沪深300 近 window 日涨幅%（10 分钟缓存，批量分析共用一次取数）。"""
    import time
    if _INDEX_RET_CACHE['ret20'] is not None and time.time() - _INDEX_RET_CACHE['ts'] < 600:
        return _INDEX_RET_CACHE['ret20']
    try:
        from core.kline_provider import fetch_index_kline
        df = fetch_index_kline('000300', window + 20)
        if df is not None and len(df) >= window + 1:
            c0 = float(df['close'].iloc[-(window + 1)])
            c1 = float(df['close'].iloc[-1])
            if c0 > 0:
                ret = round((c1 / c0 - 1) * 100, 2)
                _INDEX_RET_CACHE.update(ts=time.time(), ret20=ret)
                return ret
    except Exception:
        pass
    return _INDEX_RET_CACHE.get('ret20')


def _strong_sectors_today() -> list[str]:
    """当日数据驱动强势板块名（净额>0 且涨幅>0，按涨幅取前15）；无缓存快照返回空。"""
    try:
        from core.cache import load_sector_latest_df
        rows: list[tuple[str, float]] = []
        for kind in ('concept', 'industry'):
            for r in load_sector_latest_df(date.today(), kind) or []:
                try:
                    net = float(r.get('净额') or 0)
                    pct = float(r.get('行业-涨跌幅') or 0)
                except (TypeError, ValueError):
                    continue
                nm = str(r.get('行业') or '').strip()
                if nm and net > 0 and pct > 0:
                    rows.append((nm, pct))
        rows.sort(key=lambda x: x[1], reverse=True)
        return [nm for nm, _ in rows[:15]]
    except Exception:
        return []


def _derive_mainline_fit(
    context: dict,
    intel_events: list | None,
    code: str = '',
    name: str = '',
    sectors: list | None = None,
) -> dict:
    """主线契合信号（命中任一即可放行强趋势延续买入）。

    大主线（AI算力链）/小主线由 core.mainline 识别；RS 领先用个股20日涨幅对比沪深300；
    板块强度、相关情报为 best-effort，缺数据则为 False，不阻断也不伪造。
    """
    from core.mainline import classify_mainline, rs_leads_market
    tech = context.get('technical_profile') or {}
    sectors = sectors or context.get('sectors') or []
    strong_sectors = _strong_sectors_today()
    tier = classify_mainline(code, name, sectors, strong_sectors=strong_sectors)
    rs_lead = rs_leads_market(tech.get('ret20'), _index_ret20())
    fit = {
        'mainline_tier': tier,
        'major_mainline': tier == 'major',
        'minor_mainline': tier == 'minor',
        'rs_lead': bool(rs_lead),
        'sector_strong': False,
        'intel_hit': False,
        'is_etf': bool(is_etf(code)),
    }
    if strong_sectors and set(str(s) for s in sectors) & set(strong_sectors):
        fit['sector_strong'] = True
    fc = context.get('fund_context')
    if isinstance(fc, str) and ('净额+' in fc or '净流入' in fc):
        fit['sector_strong'] = True
    try:
        today = date.today()
        for ev in (intel_events or []):
            evd = getattr(ev, 'date', None)
            if evd and (today - evd).days <= 5:
                fit['intel_hit'] = True
                break
    except Exception:
        pass
    fit['fit'] = bool(
        fit['major_mainline'] or fit['minor_mainline'] or fit['rs_lead']
        or fit['sector_strong'] or fit['intel_hit']
    )
    return fit


def _apply_strategy_contract(prediction: dict, strategy_profile: dict | None) -> None:
    """Validate and fill the strategy contract after G, without inventing category."""
    if not isinstance(prediction, dict) or not isinstance(strategy_profile, dict) or not strategy_profile:
        return
    candidates = strategy_profile.get('strategy_candidates')
    if not isinstance(candidates, list):
        candidates = []
    selected = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    family = strategy_profile.get('strategy_family') or selected.get('strategy_family')
    name = strategy_profile.get('strategy_name') or selected.get('strategy_name')
    stage = strategy_profile.get('stage') or selected.get('stage') or 'none'
    buy_ready = bool(strategy_profile.get('buy_ready') or selected.get('buy_ready')) and stage == 'triggered'

    prediction['strategy_profile'] = strategy_profile
    if family and not prediction.get('strategy_family'):
        prediction['strategy_family'] = family
    if name and not prediction.get('strategy_name'):
        prediction['strategy_name'] = name
    if stage and not prediction.get('strategy_stage'):
        prediction['strategy_stage'] = stage
    if not prediction.get('strategy_reason'):
        prediction['strategy_reason'] = selected.get('reason') or strategy_profile.get('action_hint')

    if prediction.get('final_action') != 'buy':
        prediction['buy_strategy'] = None
        return

    if not buy_ready:
        prediction['final_action'] = 'watch_only'
        prediction['final_rating'] = 'hold'
        prediction['entry_ref'] = None
        prediction['buy_strategy'] = None
        prediction['_trade_block_reason'] = '策略原型未触发或证据不足，不允许买入'
        if not prediction.get('strategy_reason'):
            missing = '；'.join(map(str, strategy_profile.get('missing_conditions') or []))
            prediction['strategy_reason'] = missing or strategy_profile.get('action_hint')
        return

    allowed_names = {
        str(item.get('strategy_name') or '').strip()
        for item in candidates
        if isinstance(item, dict) and item.get('strategy_name')
    }
    buy_strategy = str(prediction.get('buy_strategy') or '').strip()
    if not buy_strategy:
        prediction['buy_strategy'] = name
    elif allowed_names and buy_strategy not in allowed_names:
        prediction['final_action'] = 'watch_only'
        prediction['final_rating'] = 'hold'
        prediction['entry_ref'] = None
        prediction['buy_strategy'] = None
        prediction['_trade_block_reason'] = '买入策略不在确定性候选策略中，不允许买入'


def _promote_strong_buy(prediction: dict, opportunity_profile: dict | None, strategy_profile: dict | None) -> None:
    """强势主线龙头确定性买点：S级已触发 + buy_ready + 方向看多 + 无F否决 + 盈亏比≥2 时，
    把 G 的观望(watch_only)直接提升为轻仓 buy——杜绝"单日资金/超买/主力连续性未确认"借口下的踏空。
    仅覆盖无持仓的观望态(final_action==watch_only)，持仓管理(hold/reduce)与中性/看空一律不动。"""
    if not isinstance(prediction, dict):
        return
    if prediction.get('final_action') != 'watch_only' or prediction.get('direction') != 'bullish':
        return
    if prediction.get('f_layer_veto'):
        return
    coverage = prediction.get('_intel_coverage') if isinstance(prediction.get('_intel_coverage'), dict) else {}
    if coverage.get('coverage_level') == 'missing':
        prediction['_strong_buy_block_reason'] = 'fresh_intel_missing'
        return
    op = opportunity_profile or {}
    sp = strategy_profile or {}
    if str(op.get('opportunity_grade') or '').upper() != 'S':
        return
    if (prediction.get('strategy_stage') or sp.get('stage')) != 'triggered' or not sp.get('buy_ready'):
        return
    try:
        if op.get('risk_reward') is None or float(op.get('risk_reward')) < 2.0:
            return
    except (TypeError, ValueError):
        return
    prediction.setdefault('category_ai', prediction.get('category'))
    prediction['final_action'] = 'buy'
    prediction['final_rating'] = 'buy'
    prediction['category'] = 'buy'
    prediction['_strong_buy_promoted'] = True
    prediction.pop('_trade_block_reason', None)


def _load_dipbuy_evidence() -> dict | None:
    for path in (
        _BASE_CACHE / 'dipbuy_evidence.json',
        Path(__file__).resolve().parents[1] / 'cache' / 'dipbuy_evidence.json',
    ):
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding='utf-8'))
                return data if isinstance(data, dict) else None
        except Exception:
            continue
    return None


def _apply_dipbuy_evidence_gate(
    prediction: dict,
    trade_setup: dict,
    market_phase: str | None,
    evidence: dict | None = None,
) -> None:
    if trade_setup.get('setup_name') != 'pullback_buy' or trade_setup.get('status') != 'triggered':
        return
    if prediction.get('final_action') != 'buy':
        return
    evidence = evidence if isinstance(evidence, dict) else _load_dipbuy_evidence()
    if not isinstance(evidence, dict) or not evidence.get('enabled', True):
        return

    min_trades = int(evidence.get('min_trades') or 30)
    phase = str(market_phase or evidence.get('regime') or '').strip()
    by_regime = evidence.get('by_regime') if isinstance(evidence.get('by_regime'), dict) else {}
    if phase and by_regime:
        stats = by_regime.get(phase) or {}
        basis = f'当前阶段 {phase}'
    else:
        stats = evidence.get('overall') if isinstance(evidence.get('overall'), dict) else {}
        basis = '总体样本'

    n = int(stats.get('n') or stats.get('trade_count') or 0)
    expectancy = stats.get('expectancy_r')
    try:
        expectancy_f = float(expectancy)
    except Exception:
        expectancy_f = None
    if n >= min_trades and expectancy_f is not None and expectancy_f > 0:
        prediction['dipbuy_evidence'] = {'basis': basis, 'n': n, 'expectancy_r': expectancy_f}
        return

    reason = (
        f'低吸历史证据不足或为负：{basis} n={n}, '
        f'expectancy_r={expectancy if expectancy is not None else "缺失"}，仅观察不买'
    )
    prediction['final_action'] = 'watch_only'
    prediction['final_rating'] = 'hold'
    prediction['entry_ref'] = None
    prediction['dipbuy_evidence'] = {'basis': basis, 'n': n, 'expectancy_r': expectancy_f}
    prediction['_trade_block_reason'] = reason


def _normalize_invalidation(prediction: dict, trade_setup: dict):
    if not isinstance(prediction, dict) or not isinstance(trade_setup, dict):
        return
    setup_name = trade_setup.get('setup_name')
    status = trade_setup.get('status')
    entry = trade_setup.get('entry_trigger')
    fail = trade_setup.get('fail_level')
    if setup_name == 'pullback_buy':
        if status == 'triggered' and fail:
            prediction['invalidation'] = f'跌破{fail}元（回调低点/止损）低吸逻辑失效'
        elif status == 'candidate':
            prediction['invalidation'] = (f'回踩不破{fail}元缩量企稳即低吸；跌破{fail}元候选失效'
                                           if fail else '回踩缩量企稳确认即低吸，放量阴线/破位则失效')
        return
    if status == 'candidate':
        if entry and fail:
            prediction['invalidation'] = f'放量站上{entry}元转为触发信号，或跌破{fail}元候选失效'
        elif entry:
            prediction['invalidation'] = f'放量站上{entry}元转为触发信号，否则继续观察'
        elif fail:
            prediction['invalidation'] = f'跌破{fail}元候选失效'
    elif status in ('avoid', 'failed') and fail:
        prediction['invalidation'] = f'重新站回关键位并获得量价确认，或跌破{fail}元维持回避'


def _save_reasoning_cache(code: str, reasoning: str, prediction: dict):
    try:
        _REASONING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _reasoning_cache_path(code).write_text(
            json.dumps({
                'version': _REASONING_CACHE_VERSION,
                'date': date.today().strftime('%Y-%m-%d'),
                'cached_at': datetime.now().isoformat(),
                'reasoning': reasoning,
                'prediction': prediction,
            }, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception:
        pass


def _ensure_structured_context(code: str, name: str, context: dict) -> dict:
    """补齐预测入口必须依赖的结构化画像，防止任一 UI/脚本入口漏传字段。"""
    ctx = dict(context or {})

    if not ctx.get('flow_profile') or not ctx.get('money_flow_summary'):
        try:
            from core.money_flow_provider import summarize as summarize_flow
            summary, realtime = summarize_flow(code, name=name)
            if not ctx.get('money_flow_summary'):
                ctx['money_flow_summary'] = summary
            if not ctx.get('flow_profile'):
                ctx['flow_profile'] = (realtime or {}).get('flow_profile')
        except Exception:
            pass

    if ctx.get('public_fund_evidence') is None:
        try:
            from core.public_fund_evidence import get_public_evidence
            ctx['public_fund_evidence'] = get_public_evidence(code, name=name)
        except Exception:
            pass

    if ctx.get('market_phase') is None:
        try:
            from core.market_context_provider import get_market_phase
            ctx['market_phase'] = get_market_phase(ctx.get('emotion'))
        except Exception:
            pass

    tech_profile = ctx.get('technical_profile') or {}
    kline_bad = '获取失败' in str(ctx.get('kline_summary') or '')
    tech_missing_ma40 = bool(
        tech_profile.get('available') and not (tech_profile.get('ma') or {}).get('ma40')
    )
    if (not tech_profile) or (not tech_profile.get('available')) or tech_missing_ma40 or kline_bad or not tech_profile.get('behavior_tags'):
        try:
            from core.kline_provider import fetch_daily_kline, summarize, build_technical_profile
            df = fetch_daily_kline(code)
            ctx['kline_summary'] = summarize(code, df=df)
            ctx['_kline_last_close'] = float(df['close'].iloc[-1]) if df is not None and not df.empty else None
            ctx['technical_profile'] = build_technical_profile(
                code, df=df, name=name,
                flow_profile=ctx.get('flow_profile'),
                public_evidence=ctx.get('public_fund_evidence'),
                market_phase=ctx.get('market_phase'),
            )
        except Exception:
            pass

    if ctx.get('restricted_release') is None:
        try:
            from core.restricted_release_provider import fetch_restricted_release
            ctx['restricted_release'] = fetch_restricted_release(code)
        except Exception:
            pass

    if ctx.get('hot_rank') is None:
        try:
            from core.data_orchestrator import _fetch_hot_rank
            ctx['hot_rank'] = _fetch_hot_rank(code)
        except Exception:
            pass

    return ctx








def load_today_predictions() -> list[dict]:
    """读取今日所有预测缓存记录，按时间倒序。"""
    today_str = date.today().strftime('%Y-%m-%d')
    cache = _load_predictions_cache()
    items = [
        v for v in cache.values()
        if isinstance(v, dict)
        and v.get('date') == today_str
        and v.get('version') == _REASONING_CACHE_VERSION
    ]
    items.sort(key=lambda x: x.get('time', ''), reverse=True)
    return items


def _match_intel_ref(ref_title: str, intel_events) -> str | None:
    """按标题模糊匹配 intel_events，返回最优 event.id；无匹配返回 None。

    优先级：6 位股票代码精确匹配 > 标题相等/包含 > 前 6 字符包含。
    """
    if not ref_title or not intel_events:
        return None
    ref = ref_title.strip()

    # 优先：6 位股票代码精确匹配（龙虎榜/大宗交易场景）
    ref_codes = set(_CODE_RE.findall(ref))
    if ref_codes:
        for ev in intel_events:
            t = ev.title if hasattr(ev, 'title') else str(ev.get('title', ''))
            eid = ev.id if hasattr(ev, 'id') else str(ev.get('id', ''))
            if ref_codes & set(_CODE_RE.findall(t)):
                return eid

    # 兜底：原有标题匹配规则
    for ev in intel_events:
        t = ev.title if hasattr(ev, 'title') else str(ev.get('title', ''))
        eid = ev.id if hasattr(ev, 'id') else str(ev.get('id', ''))
        if ref == t or ref in t or t in ref:
            return eid
    if len(ref) >= 6:
        for ev in intel_events:
            t = ev.title if hasattr(ev, 'title') else str(ev.get('title', ''))
            eid = ev.id if hasattr(ev, 'id') else str(ev.get('id', ''))
            if ref[:6] in t:
                return eid
    return None


# ════════════════════════════════════════════════════════════════════
# v3.0 — 5 Agent + F/Z/G 编排（predict_unified）
# ════════════════════════════════════════════════════════════════════

def predict_unified(
    code: str,
    name: str,
    api_key: str,
    intel_events: list,
    context: dict,
    current_price: float | None = None,
    kind: str = 'stock',
    profile: dict | None = None,
    stock_news: list[dict] | None = None,
    sectors: list[str] | None = None,
    force_refresh: bool = False,
    cognition: dict | None = None,
) -> tuple[dict | None, str, str, str]:
    """v3.0 统一预测入口 — 5 Agent + F 多空辩论 + Z 时间约束 + G 决策。

    Args:
        code, name: 目标
        api_key: DeepSeek key
        intel_events: 相关 IntelEvent 列表（已由上游 filter_relevant_intel 过滤）
        context: 由 data_orchestrator.gather_stock_context 提供
                 包含 kline_summary / fundamental_summary / money_flow_summary
                 emotion / global / fund_context / restricted_release / hot_rank
        current_price: 当前价
        kind: 'stock'（默认）暂不支持 'sector'（板块走 intel_fetcher.analyze_sector）
        profile: 用户画像 dict；None 时自动调 user_profile.load_profile()
        stock_news: 个股专属新闻列表（由 stock_news_provider 提供）；可为 None
        sectors: 该股所属板块列表（intel_matcher.infer_sectors_for_stock）；可为 None
        force_refresh: True 时跳过 4h 推理层缓存
        cognition: 用户本次填写的持仓认知；仅用于 Z 层期限覆盖和 G 层后的确定性持仓建议

    Returns:
        (prediction_dict, reasoning_chain, cache_key, error_msg)
        prediction 额外字段：_agent_macro / _agent_company /
                              _agent_technical / _agent_fundflow / _agent_news /
                              _debate / _horizon_directive
    """
    from concurrent.futures import ThreadPoolExecutor
    from core.agents import (
        run_macro_agent, run_company_agent, run_technical_agent,
        run_fundflow_agent, run_news_event_agent,
        run_debate, build_horizon_directive, run_decision,
    )
    from core.user_profile import load_profile, build_profile_summary
    from core.analysis_memory import archive_prediction

    today_str = date.today().strftime('%Y-%m-%d')
    now_time = datetime.now().strftime('%H:%M:%S')
    cache_key = f'{code}_{today_str}_{now_time.replace(":", "")}'
    context = _ensure_structured_context(code, name, context or {})
    if sectors and not context.get('sectors'):
        context['sectors'] = list(sectors)
    trade_setup = context.get('trade_setup') or derive_trade_setup(context)
    context['trade_setup'] = trade_setup
    context.setdefault('mainline_fit', _derive_mainline_fit(context, intel_events, code, name, sectors))
    context['intel_coverage'] = build_intel_coverage(stock_news or [], intel_events or [])
    opportunity_profile = context.get('opportunity_profile') or derive_opportunity_profile(context, trade_setup)
    context['opportunity_profile'] = opportunity_profile
    try:
        from core.strategy_profile import derive_strategy_profile
        strategy_profile = context.get('strategy_profile') or derive_strategy_profile(
            context, trade_setup, opportunity_profile
        )
        context['strategy_profile'] = strategy_profile
    except Exception:
        strategy_profile = context.get('strategy_profile') or {}

    # 推理层 4h 缓存
    if not force_refresh:
        rc = _load_reasoning_cache(code)
        if rc is not None:
            prediction = dict(rc['prediction'])
            cached_tech = prediction.get('_agent_technical') or {}
            cached_tech_bad = (
                isinstance(cached_tech, dict)
                and (
                    cached_tech.get('confidence', 0) <= 0
                    or not str(cached_tech.get('summary') or '').strip()
                    or '获取失败' in str(cached_tech.get('summary') or '')
                )
            )
            current_tech_ok = bool((context.get('technical_profile') or {}).get('available'))
            cached_setup = prediction.get('_trade_setup') or {}
            cached_setup_stale = (
                current_tech_ok
                and isinstance(cached_setup, dict)
                and cached_setup.get('reason') == '技术画像不可用'
            )
            cached_agent_names_stale = any(
                isinstance(prediction.get(key), dict)
                and prediction.get(key, {}).get('analyst') in ('宏观/情报', '新闻/事件')
                for key in ('_agent_macro', '_agent_news')
            )
            cached_tech_no_ma40 = (
                isinstance(cached_tech, dict)
                and 'MA40' not in str(cached_tech)
                and bool((context.get('technical_profile') or {}).get('available'))
            )
            if cached_tech_bad or cached_setup_stale or cached_agent_names_stale or cached_tech_no_ma40:
                rc = None
        if rc is not None:
            prediction = dict(rc['prediction'])
            reasoning = rc.get('reasoning', '')
            if current_price:
                prediction['entry_price'] = current_price
                if prediction.get('direction') != 'neutral':
                    prediction['entry_ref'] = current_price
            prediction['from_reasoning_cache'] = True
            if strategy_profile:
                prediction['strategy_profile'] = strategy_profile
                prediction['strategy_family'] = strategy_profile.get('strategy_family')
                prediction['strategy_stage'] = strategy_profile.get('stage')
            _apply_prediction_policy_metadata(
                prediction,
                context,
                stock_news,
                intel_events,
                {
                    'macro': prediction.get('_agent_macro'),
                    'company': prediction.get('_agent_company'),
                    'technical': prediction.get('_agent_technical'),
                    'fundflow': prediction.get('_agent_fundflow'),
                    'news': prediction.get('_agent_news'),
                },
            )
            _apply_trade_setup_constraints(
                prediction, trade_setup, cognition, opportunity_profile,
                market_phase=context.get('market_phase'),
            )
            _promote_strong_buy(prediction, opportunity_profile, strategy_profile)
            _apply_strategy_contract(prediction, strategy_profile)
            _normalize_invalidation(prediction, trade_setup)
            _enforce_tradeability(prediction)
            _sync_category(prediction)
            prediction['decision_view'] = _derive_decision_view(prediction, trade_setup, opportunity_profile, context)
            _apply_etf_final_cap(prediction, is_etf(code), current_price)
            prediction['task_blueprint'] = _assemble_task_blueprint(prediction, trade_setup)
            try:
                prediction['horizon_date'] = _add_trading_days(FORCED_HORIZON_DAYS)
            except Exception:
                pass
            if cognition:
                profile_for_advice = profile or load_profile()
                cognition = dict(cognition)
                cognition['_profile_horizon'] = _profile_horizon(profile_for_advice)
                prediction['_holding_advice'] = _build_holding_advice(prediction, cognition)
            return prediction, reasoning, cache_key, ''

    if profile is None:
        profile = load_profile()
    profile_summary = build_profile_summary(profile)
    if cognition is not None:
        cognition = dict(cognition)
        cognition['_profile_horizon'] = _profile_horizon(profile)

    # ── 5 Agent 并行（V4.1 Flash thinking）───────────────────────
    macro_text = company_text = tech_text = fundflow_text = news_text = ''
    agent_failures: list[str] = []
    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            _futures = {
                'macro':   ex.submit(run_macro_agent, api_key, code, name, current_price, context, intel_events),
                'company':  ex.submit(run_company_agent, api_key, 'stock',
                                      code=code, name=name,
                                      fundamental_summary=context.get('fundamental_summary', ''),
                                      sectors=sectors or [],
                                      restricted_release=context.get('restricted_release'),
                                      fund_context=context.get('fund_context', '')),
                'tech':     ex.submit(run_technical_agent, api_key, code, name, current_price, context),
                'fundflow': ex.submit(run_fundflow_agent, api_key, code, name, context),
                'news':     ex.submit(run_news_event_agent, api_key, code, name, stock_news or [], context),
            }
            _results: dict[str, str] = {}
            for _name, _fut in _futures.items():
                try:
                    _results[_name] = _fut.result()
                except Exception as _e:
                    agent_failures.append(f'{_name}: {_e}')
                    _results[_name] = ''
            macro_text    = _results['macro']
            company_text  = _results['company']
            tech_text     = _results['tech']
            fundflow_text = _results['fundflow']
            news_text     = _results['news']
    except Exception as _e:
        agent_failures.append(f'executor: {_e}')

    if len(agent_failures) >= 5:
        return None, '', '', f'所有Agent失败: {"; ".join(agent_failures[:5])}'

    # 构建 intel_refs 标题→事件ID 映射（供 UI 可点击跳转）
    _intel_ref_ids: dict[str, str] = {}
    if intel_events:
        for _agent_d in [macro_text, company_text, tech_text, fundflow_text, news_text]:
            if not isinstance(_agent_d, dict):
                continue
            for _ref in (_agent_d.get('intel_refs') or []):
                if _ref and _ref not in _intel_ref_ids:
                    _eid = _match_intel_ref(_ref, intel_events)
                    if _eid:
                        _intel_ref_ids[_ref] = _eid

    agent_reports = {
        'macro': macro_text,
        'company': company_text,
        'technical': tech_text,
        'fundflow': fundflow_text,
        'news': news_text,
    }
    calibration_context = build_agent_calibration_context(
        context,
        stock_news or [],
        intel_events or [],
        agent_reports,
    )
    context['intel_coverage'] = calibration_context.get('intel_coverage') or context.get('intel_coverage')

    # ── F 层 多空辩论（pro）──────────────────────────────
    debate_text, debate_reasoning = run_debate(
        api_key, code, name, kind,
        macro_text, company_text, tech_text,
        news_report=news_text, fundflow_report=fundflow_text,
        trade_setup=trade_setup, opportunity_profile=opportunity_profile,
        strategy_profile=strategy_profile,
        calibration_context=calibration_context,
    )

    # ── Z 层 时间约束（确定性，无 LLM）──────────────────
    horizon_profile = _effective_profile(profile, cognition)
    horizon_directive = build_horizon_directive(horizon_profile, code=code, sectors=sectors or [])

    # ── G 层 最终决策（V4.1 Flash thinking）────────────────────
    prediction, decision_reasoning, err = run_decision(
        api_key, code, name, kind, current_price,
        macro_text, {}, company_text, tech_text,
        debate_text, horizon_directive, profile_summary,
        news_report=news_text, fundflow_report=fundflow_text,
        trade_setup=trade_setup, opportunity_profile=opportunity_profile,
        strategy_profile=strategy_profile, cognition=cognition,
        calibration_context=calibration_context,
    )

    if prediction is None:
        return None, decision_reasoning or debate_reasoning, '', err or 'G 层决策失败'

    # 补充元信息
    prediction['code'] = code
    prediction['name'] = name
    prediction['sectors'] = sectors or []
    prediction['created_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    prediction['prediction_id'] = cache_key
    prediction['_agent_macro'] = macro_text
    prediction['_agent_company'] = company_text
    prediction['_agent_technical'] = tech_text
    prediction['_agent_fundflow'] = fundflow_text
    prediction['_agent_news'] = news_text
    prediction['_restricted_release'] = context.get('restricted_release')
    prediction['_debate'] = debate_text
    _f_dir, _f_conf = parse_f_layer_verdict(debate_text)
    prediction['f_layer_direction'] = _f_dir
    prediction['f_layer_confidence'] = _f_conf
    prediction['f_layer_veto'] = bool(_f_dir == 'bearish' and (_f_conf or 0) >= 6)
    prediction['_horizon_directive'] = horizon_directive
    prediction['_intel_ref_ids'] = _intel_ref_ids
    prediction['_trade_setup'] = trade_setup
    prediction['_opportunity_profile'] = opportunity_profile
    _apply_prediction_policy_metadata(
        prediction,
        context,
        stock_news,
        intel_events,
        agent_reports,
        calibration_context,
    )
    if strategy_profile:
        prediction['strategy_profile'] = strategy_profile
        prediction['strategy_family'] = strategy_profile.get('strategy_family')
        prediction['strategy_name'] = strategy_profile.get('strategy_name')
        prediction['strategy_stage'] = strategy_profile.get('stage')

    _apply_trade_setup_constraints(
        prediction, trade_setup, cognition, opportunity_profile,
        market_phase=context.get('market_phase'),
    )
    _promote_strong_buy(prediction, opportunity_profile, strategy_profile)
    _apply_strategy_contract(prediction, strategy_profile)
    _normalize_invalidation(prediction, trade_setup)

    _no_entry = prediction.get('direction') == 'neutral' or prediction.get('final_action') in ('watch_only', 'not_suitable')
    if _no_entry:
        prediction['entry_ref'] = None
    elif current_price and prediction.get('entry_ref') is None:
        prediction['entry_ref'] = current_price
    prediction['entry_price'] = prediction.get('entry_ref') or current_price or 0
    _enforce_tradeability(prediction)
    _sync_category(prediction)
    prediction['decision_view'] = _derive_decision_view(prediction, trade_setup, opportunity_profile, context)

    # 方案 B：非 neutral 方向若 target/stop 缺失，从 Agent4 技术报告提取兜底
    if prediction.get('direction') in ('bullish', 'bearish') and prediction.get('final_action') != 'not_suitable':
        if not prediction.get('target_pct') or not prediction.get('stop_pct'):
            _fallback_target, _fallback_stop = _extract_agent4_levels(tech_text, current_price)
            if not prediction.get('target_pct'):
                prediction['target_pct'] = _fallback_target
            if not prediction.get('stop_pct'):
                prediction['stop_pct'] = _fallback_stop

    # 盈亏比 ≥ 2.0 硬约束兜底（G层 _normalize 可能未执行到）
    if prediction.get('direction') in ('bullish', 'bearish') and prediction.get('final_action') != 'not_suitable':
        _t = prediction.get('target_pct') or 0
        _s = prediction.get('stop_pct') or 0
        if _s > 0 and _t / _s < 2.0:
            prediction['target_pct'] = round(_s * 2.0, 1)

    if prediction.get('direction') in ('bullish', 'bearish') and prediction.get('final_action') != 'not_suitable':
        _tp = _safe_float(prediction.get('target_pct'))
        if _tp > 0:
            from core.kline_provider import short_term_target_cap_pct
            _ind = (context.get('technical_profile') or {}).get('indicators') or {}
            _atr = _safe_float(_ind.get('atr14'))
            _hz = normalize_horizon_days(prediction.get('horizon_days'))
            _cap = short_term_target_cap_pct(_hz)
            _ceiling = (_atr / current_price * 100 * _hz * 0.9) if (_atr > 0 and current_price) else _cap
            _new_tp = min(_tp, _cap, _ceiling)
            _sp = _safe_float(prediction.get('stop_pct'))
            if _sp > 0:
                _new_sp = max(min(_sp, _new_tp / 2.0), 3.0)
                if _new_tp < _new_sp * 2:
                    _new_tp = round(_new_sp * 2.0, 1)
                prediction['stop_pct'] = round(_new_sp, 1)
            prediction['target_pct'] = round(_new_tp, 1)

    _apply_etf_final_cap(prediction, is_etf(code), current_price)
    prediction['task_blueprint'] = _assemble_task_blueprint(prediction, trade_setup)

    prediction['_holding_advice'] = _build_holding_advice(prediction, cognition)

    entry = prediction['entry_price']
    if entry and entry > 0 and not _no_entry:
        direction = prediction.get('direction', 'neutral')
        target_pct = prediction.get('target_pct', 0) or 0
        stop_pct = prediction.get('stop_pct', 0) or 0
        if direction == 'bullish':
            prediction['win_price'] = round(entry * (1 + target_pct / 100), 3)
            prediction['lose_price'] = round(entry * (1 - abs(stop_pct) / 100), 3)
        elif direction == 'bearish':
            prediction['win_price'] = round(entry * (1 - target_pct / 100), 3)
            prediction['lose_price'] = round(entry * (1 + abs(stop_pct) / 100), 3)
        try:
            prediction['horizon_date'] = _add_trading_days(FORCED_HORIZON_DAYS)
        except Exception:
            pass

    # P1.3 信念快照
    try:
        def _top_ev(agent_d, n=2):
            if not isinstance(agent_d, dict):
                return []
            ev = agent_d.get('evidence') or agent_d.get('signals') or []
            high = [e for e in ev if e.get('weight') == 'high']
            src = high if high else ev
            return [{'name': e.get('name', ''), 'value': e.get('value', '')} for e in src[:n]]
        top_evidence = []
        top_counter = []
        for _ad in [macro_text, company_text, tech_text, fundflow_text, news_text]:
            top_evidence.extend(_top_ev(_ad, 2))
            if isinstance(_ad, dict):
                ce = _ad.get('counter_evidence') or []
                top_counter.extend([{'name': e.get('name', ''), 'value': e.get('value', '')} for e in ce[:1]])
        prediction['belief_snapshot'] = {
            'main_thesis': str(prediction.get('thesis', ''))[:200],
            'top_evidence': top_evidence[:5],
            'counter_evidence': top_counter[:3],
            'uncertainties': str(prediction.get('invalidation', '')),
            'technical_behavior_tags': (context.get('technical_profile') or {}).get('behavior_tags') or [],
            'public_fund_evidence': context.get('public_fund_evidence'),
            'market_phase': context.get('market_phase') or 'unknown',
        }
    except Exception:
        prediction['belief_snapshot'] = None

    # 用户可见原始思考：仅 F 层辩论（市场分析），G 层含用户画像不外露
    full_reasoning = f'[多空辩论原始思考]\n{debate_reasoning}' if debate_reasoning else ''

    # 写预测缓存（读-改-写加锁，支持批量并发分析）
    with _PRED_CACHE_LOCK:
        cache = _load_predictions_cache()
        cache[cache_key] = {
            'version': _REASONING_CACHE_VERSION,
            'date': today_str,
            'code': code,
            'name': name,
            'prediction': prediction,
            'reasoning': full_reasoning,
            'time': now_time,
        }
        _save_predictions_cache(cache)
    _save_reasoning_cache(code, full_reasoning, prediction)

    # 归档到 RAG 记忆（Step B 闭环）— 不阻塞主流程
    try:
        archive_prediction(prediction)
    except Exception:
        pass

    return prediction, full_reasoning, cache_key, ''


def predict_sector(name: str, api_key: str, force_refresh: bool = False) -> tuple[dict | None, str, str, str]:
    """板块预测入口 — 调用 intel_fetcher.analyze_sector 并解析为结构化预测。

    Returns:
        (prediction_dict, reasoning, cache_key, error_msg)
    """
    import re as _re
    from core.intel_fetcher import analyze_sector
    from core.intel_matcher import load_intel_feed

    # 加载全量情报（板块分析不过滤，由 LLM 自行筛选相关项）
    try:
        all_events = load_intel_feed()
    except Exception:
        all_events = []
    intel_events = list(all_events) if all_events else []

    # 板块快速分析模式 context
    context: dict = {
        'fund_flow': None,
        'timeline': [],
        'emotion': None,
        'minute': None,
        'constituents': None,
        'top_sectors': [],
    }

    reasoning, result, err, cache_key, _now = analyze_sector(
        sector_name=name,
        kind='concept',
        api_key=api_key,
        intel_events=intel_events,
        context=context,
    )

    if err:
        return None, '', '', err

    # 解析 JSON spec
    pred: dict | None = None
    m = _re.search(r'```json\s*(\{[\s\S]*?\})\s*```', result)
    if m:
        try:
            spec = json.loads(m.group(1))
            pred = {
                'direction': spec.get('direction', 'neutral'),
                'confidence': float(spec.get('confidence', 5)) / 10.0,
                'target_pct': spec.get('target_pct', 5.0),
                'stop_pct': spec.get('stop_pct', 2.5),
                'horizon_days': FORCED_HORIZON_DAYS,
                'thesis': spec.get('thesis', '') or result[:300],
            }
        except Exception:
            pred = None

    if pred is None:
        # fallback: 无法解析JSON时返回中性
        pred = {
            'direction': 'neutral', 'confidence': 0.5,
            'target_pct': 5.0, 'stop_pct': 2.5,
            'horizon_days': FORCED_HORIZON_DAYS, 'thesis': result[:300],
        }

    return pred, reasoning, cache_key, ''
