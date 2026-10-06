"""预测追踪任务系统 — Aldebaran v3.0。

功能：
  - AI 分析完自动创建追踪任务（不需用户手动）
  - 坚持到审判日（deadline）再结算，不提前终止
  - ABCDEF 评分：outcome_tier × path_quality 二维矩阵
  - AI 反思 prompt 构建（供 _RetrospectiveWorker 调用）
  - 首次启动自动迁移旧 virtual_portfolio.json 的 open 记录

落盘：BASE_DIR/tracking_tasks.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from core.paths import HOME, CACHE_DIR
from core.tracking_display import (
    format_directional_metric_lines,
    format_retrospective_metric_lines,
    reconcile_view_to_category,
)
_DATA_FILE = HOME / 'tracking_tasks.json'
_EVENT_LOG_FILE = HOME / 'tracking_events.jsonl'
_IMPROVEMENT_FILE = HOME / 'improvement_candidates.json'
_ISO_DATE_RE = re.compile(r'20\d{2}-\d{2}-\d{2}')


def _latest_available_trade_date(today: date | None = None) -> str:
    now = datetime.now()
    d = today or now.date()
    if today is None and (now.hour, now.minute) < (9, 30):
        d -= timedelta(days=1)
    try:
        from core.trade_calendar import is_trade_day
        for _ in range(20):
            if is_trade_day(d):
                return d.isoformat()
            d -= timedelta(days=1)
    except Exception:
        pass
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _latest_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        for key in ('date', 'datetime', 'time', 'cached_at', 'checked_at'):
            found = _latest_iso_date(value.get(key))
            if found:
                return found
        return None
    matches = _ISO_DATE_RE.findall(str(value))
    return max(matches) if matches else None


def _prediction_has_freshness_inputs(pred: dict) -> bool:
    keys = (
        'from_reasoning_cache', '_kline_summary', '_flow_profile',
        '_money_flow_summary', '_agent_technical', '_agent_fundflow',
        '_stock_news_checked_at', '_intel_checked_at', '_stock_news_error',
        '_intel_check_error',
    )
    return any(key in pred for key in keys)


def _validate_prediction_freshness(pred: dict) -> None:
    if not _prediction_has_freshness_inputs(pred):
        return

    expected = _latest_available_trade_date()
    issues: list[str] = []

    if pred.get('from_reasoning_cache'):
        issues.append('stale_reasoning_cache')

    kline_date = _latest_iso_date(pred.get('_kline_summary')) or _latest_iso_date(pred.get('_agent_technical'))
    if not kline_date or kline_date < expected:
        issues.append(f'stale_kline latest={kline_date or "missing"} required={expected}')

    flow_profile = pred.get('_flow_profile') if isinstance(pred.get('_flow_profile'), dict) else {}
    flow_date = (
        _latest_iso_date((flow_profile.get('last') or {}))
        or _latest_iso_date(pred.get('_money_flow_summary'))
        or _latest_iso_date(pred.get('_agent_fundflow'))
    )
    if not flow_date or flow_date < expected:
        issues.append(f'stale_flow latest={flow_date or "missing"} required={expected}')

    stock_news_checked = _latest_iso_date(pred.get('_stock_news_checked_at'))
    intel_checked = _latest_iso_date(pred.get('_intel_checked_at'))
    if (
        pred.get('_stock_news_error')
        or pred.get('_intel_check_error')
        or not stock_news_checked
        or stock_news_checked < expected
        or not intel_checked
        or intel_checked < expected
    ):
        issues.append(
            f'stale_intel stock_news_checked={stock_news_checked or "missing"} '
            f'intel_checked={intel_checked or "missing"} required={expected}'
        )

    if issues:
        raise ValueError('输入数据不新鲜: ' + '; '.join(issues))


# ─────────────────────────────────────────────
# CRUD
# ─────────────────────────────────────────────

def _reconcile_task_view(task: dict) -> None:
    dv = task.get('decision_view')
    if not isinstance(dv, dict) or not dv.get('bias_label'):
        return
    ps = task.get('pred_snapshot') or {}
    category = (ps.get('task_blueprint') or {}).get('category')
    if task.get('converted_from_watch'):
        category = 'buy'
    if category not in ('buy', 'avoid', 'watch', 'bullish_watch'):
        return
    veto = bool((ps.get('task_blueprint') or {}).get('f_layer_veto'))
    dv['bias_label'], dv['readiness'], dv['evaluation_scope'] = reconcile_view_to_category(
        category, dv.get('bias_label', ''), dv.get('readiness', ''),
        dv.get('evaluation_scope', ''), ps.get('opportunity_grade') or '',
        f_layer_veto=veto,
    )


def load_tasks() -> list[dict]:
    """读取所有追踪任务。"""
    try:
        if _DATA_FILE.exists():
            tasks = json.loads(_DATA_FILE.read_text(encoding='utf-8'))
            for t in tasks:
                t.setdefault('error_type', None)
                t.setdefault('black_swan_flag', False)
                t.setdefault('belief_snapshot', None)
                t.setdefault('neutral_type', None)
                t.setdefault('user_suitability', None)
                t.setdefault('final_action', None)
                t.setdefault('decision_view', (t.get('pred_snapshot') or {}).get('decision_view') or {})
                t.setdefault('expected_range_pct', None)
                t.setdefault('root_cause', None)
                t.setdefault('strategy_tag', _infer_strategy_tag(t.get('pred_snapshot') or t))
                t.setdefault('risk_flags', _infer_risk_flags(t.get('pred_snapshot') or t))
                t['sample_type'] = 'watch'
                t.setdefault('source', 'local')
                _reconcile_task_view(t)
            return tasks
    except Exception as e:
        logger.warning('[tracking] 读取失败: %s', e)
    return []


_FILE_LOCK = threading.RLock()


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def save_tasks(tasks: list[dict]) -> None:
    with _FILE_LOCK:
        try:
            _write_json_atomic(_DATA_FILE, tasks)
        except Exception as e:
            logger.warning('[tracking] 保存失败: %s', e)


def save_tasks_merge(tasks: list[dict]) -> None:
    """合并保存：worker 持有的任务按内存版写，磁盘上 worker 没见过的任务保留。

    后台 worker 持旧快照整体写回会覆盖主线程同期新建的任务（lost-update）。
    锁内重新 load 磁盘，磁盘上 id 不在内存列表里的任务（= 他处新增）追加保留，
    内存里的任务全按 worker 处理结果写入。worker 路径只增改不删，故安全。
    """
    with _FILE_LOCK:
        mem_ids = {str(t.get('id')) for t in tasks}
        disk = load_tasks()
        extra = [t for t in disk if str(t.get('id')) not in mem_ids]
        save_tasks(list(tasks) + extra)


def persist_task_fields(task_id, fields: dict) -> dict | None:
    """锁内读最新磁盘，只更新单个任务的指定字段后写回。

    主线程持旧列表整体覆盖会回退后台 worker 同期的结算结果，
    单字段更新（如复盘文本）改走此函数，以磁盘最新为准只动这一条。
    返回更新后的任务（磁盘版），未找到返回 None。
    """
    with _FILE_LOCK:
        tasks = load_tasks()
        target = None
        for t in tasks:
            if str(t.get('id')) == str(task_id):
                t.update(fields)
                target = t
                break
        if target is not None:
            save_tasks(tasks)
        return target


def append_tracking_event(
    event_type: str,
    *,
    task_id: str = '',
    code: str = '',
    name: str = '',
    source: str = 'local',
    severity: str = 'info',
    message: str = '',
    payload: dict | None = None,
) -> None:
    """追加一条追踪审计日志。

    这是 append-only JSONL；未来可在报告里扩展因子归因摘要，但本轮不落因子字段。
    """
    event = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'event_type': str(event_type or ''),
        'task_id': str(task_id or ''),
        'code': str(code or ''),
        'name': str(name or ''),
        'source': str(source or 'local'),
        'severity': str(severity or 'info'),
        'message': str(message or ''),
        'payload': payload if isinstance(payload, dict) else {},
    }
    try:
        _EVENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _EVENT_LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
    except Exception as e:
        logger.warning('[tracking] 写入审计日志失败: %s', e)


def load_tracking_events(limit: int | None = None) -> list[dict]:
    """读取追踪审计日志，坏行跳过。"""
    if not _EVENT_LOG_FILE.exists():
        return []
    events: list[dict] = []
    try:
        lines = _EVENT_LOG_FILE.read_text(encoding='utf-8').splitlines()
    except Exception as e:
        logger.warning('[tracking] 读取审计日志失败: %s', e)
        return []
    if limit is not None and limit > 0:
        lines = lines[-limit:]
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def get_open_tasks(tasks: list[dict] | None = None) -> list[dict]:
    t = tasks if tasks is not None else load_tasks()
    return [x for x in t if x.get('status') == 'open']


def get_closed_tasks(tasks: list[dict] | None = None) -> list[dict]:
    t = tasks if tasks is not None else load_tasks()
    return [x for x in t if x.get('status') == 'closed']


def _confidence_0_10(pred: dict) -> float:
    try:
        conf = float(pred.get('confidence', 0) or 0)
    except Exception:
        return 0.0
    return conf


def _infer_strategy_tag(pred: dict) -> str:
    text_parts = [
        str(pred.get('thesis', '') or ''),
        str(pred.get('reasoning', '') or ''),
    ]
    for key in ('_agent_macro', '_agent_company', '_agent_technical',
                '_agent_fundflow', '_agent_news', '_debate'):
        val = pred.get(key)
        if isinstance(val, dict):
            text_parts.append(json.dumps(val, ensure_ascii=False))
        else:
            text_parts.append(str(val or ''))
    text = '\n'.join(text_parts)
    if any(w in text for w in ('主力', '资金流', '超大单', 'OBV', '放量')):
        return '资金驱动'
    if any(w in text for w in ('情报', '政策', '消息', '新闻', '订单', '催化')):
        return '情报驱动'
    if any(w in text for w in ('突破', '均线多头', 'MACD', 'RSI', 'KDJ', '布林')):
        return '技术驱动'
    if any(w in text for w in ('ROE', '净利润', '营收', '毛利率', '财报', '季报', '基本面')):
        return '财务驱动'
    if any(w in text for w in ('板块', '轮动', '景气度', '产业链')):
        return '板块轮动'
    return '未分类'


def _infer_risk_flags(pred: dict) -> list[str]:
    flags: list[str] = []
    direction = str(pred.get('direction', '') or '')
    action = str(pred.get('final_action', '') or '')
    entry = pred.get('entry_price') or pred.get('entry_ref') or 0
    target = pred.get('target_pct')
    stop = pred.get('stop_pct')
    conf = _confidence_0_10(pred)
    text = ' '.join(str(pred.get(k, '') or '') for k in ('thesis', 'reasoning', 'invalidation'))
    decision_view = pred.get('decision_view') or {}
    readiness = decision_view.get('readiness', '')
    is_watch_bullish = action in ('watch_only', 'hold') and direction == 'bullish'

    # 方向/动作标记
    if direction in ('neutral', '观望'):
        flags.append('中性观望')
    elif is_watch_bullish:
        flags.append('未触发观察')
    elif action in ('watch_only', 'not_suitable'):
        flags.append('观望信号')
    elif action == 'hold':
        flags.append('持仓观察')

    if direction == 'bearish' and action not in ('sell', 'hold', 'watch_only', 'reduce'):
        flags.append('A股看空不宜当开仓单')
    if direction == 'bearish' and not entry:
        flags.append('看空无入场价')
    if conf and conf < 6:
        flags.append('置信度不足')
    # watch_only + bullish + entry_ref=null 是正常的未触发状态，不标"缺少入场价"
    if not entry and direction not in ('neutral', 'bearish', '观望') and not is_watch_bullish:
        flags.append('缺少入场价')
    skip_plan_flag = direction in ('neutral', '观望') or action in ('watch_only', 'not_suitable')
    if not skip_plan_flag and (target is None or stop is None or float(target or 0) <= 0 or float(stop or 0) == 0):
        flags.append('目标止损不完整')
    elif not skip_plan_flag:
        try:
            if float(target) / abs(float(stop)) < 2.0:
                flags.append('盈亏比不足')
        except Exception:
            flags.append('目标止损不完整')
    if any(w in text for w in ('超买', 'RSI 7', 'RSI>7', 'KDJ超买', '高位')):
        flags.append('技术过热')
    if any(w in text for w in ('资金流出', '主力卖出', '散户接盘')):
        flags.append('资金背离')
    rr = pred.get('_restricted_release')
    if isinstance(rr, dict):
        try:
            if float(rr.get('ratio', 0) or 0) >= 5.0:
                flags.append('解禁压力')
        except Exception:
            pass
    return list(dict.fromkeys(flags))


# 只有这些 flag 才强制归为观察单；技术过热/资金背离仅作警示不拦截
_HARD_BLOCK_FLAGS = frozenset({
    '观望信号', 'A股看空不宜当开仓单', '看空无入场价',
    '置信度不足', '缺少入场价', '目标止损不完整', '盈亏比不足',
})


def _infer_sample_type(pred: dict, risk_flags: list[str]) -> str:
    """All tracking records are observation samples; this app does not track real trades."""
    return 'watch'


def _norm_code(code) -> str:
    return re.sub(r'^(SH|SZ|BJ)', '', str(code or ''))


def create_task(pred: dict, intel_events: list) -> dict:
    """从 predictor 返回的 pred_dict 创建追踪任务并持久化，返回新任务 dict。

    pred 必须含有效 task_blueprint（AI↔跟踪唯一契约），否则拒单。
    """
    bp = pred.get('task_blueprint')
    if not (isinstance(bp, dict) and bp.get('category')):
        raise ValueError(f"无有效 task_blueprint，拒绝建单: {pred.get('_blueprint_error') or 'missing'}")
    _validate_prediction_freshness(pred)
    tasks = load_tasks()
    today = date.today()
    from core.prediction_policy import normalize_horizon_days
    horizon_days = normalize_horizon_days(bp.get('horizon_days'))
    bp['horizon_days'] = horizon_days
    pred['horizon_days'] = horizon_days

    # 固定用当前建单日重新计算审判日，避免复用缓存或模型返回的陈旧 horizon_date。
    try:
        from core.trade_calendar import add_trading_days
        deadline = add_trading_days(horizon_days)
    except Exception:
        deadline = (today + timedelta(days=horizon_days)).isoformat()

    # 情报快照（序列化 IntelEvent）
    code = str(pred.get('code', '')).strip()
    name = str(pred.get('name', '')).strip()
    intel_snap = _serialize_intel_snapshot(intel_events, code, name)
    risk_flags = _infer_risk_flags(pred)
    strategy_tag = _infer_strategy_tag(pred)
    strategy_profile = pred.get('strategy_profile') if isinstance(pred.get('strategy_profile'), dict) else {}
    sample_type = _infer_sample_type(pred, risk_flags)

    task: dict[str, Any] = {
        'id': _next_task_id(today.strftime('%Y%m%d'), tasks),
        'created_at': datetime.now().isoformat(timespec='seconds'),
        'kind': str(pred.get('kind', 'stock')),
        'final_rating': str(pred.get('final_rating', 'hold')),
        'intel_refs': list(pred.get('intel_refs', []) or []),
        'code': code,
        'name': name,
        'direction': bp['direction'],
        'entry_price': float(bp['entry_price']),
        'target_pct': float(bp.get('target_pct', 0) or 0),
        'stop_pct': float(bp.get('stop_pct', 0) or 0),
        'horizon_days': horizon_days,
        'deadline': deadline,
        'confidence': float(pred.get('confidence', 0.5)),
        'reasoning': str(pred.get('reasoning', '')),
        'pred_snapshot': _safe_copy(pred),
        'intel_snapshot': intel_snap,
        'intel_coverage': _safe_copy(pred.get('_intel_coverage') or pred.get('intel_coverage') or {}),
        'neutral_type': pred.get('neutral_type'),
        'expected_range_pct': bp.get('expected_range'),
        'user_suitability': pred.get('user_suitability'),
        'final_action': pred.get('final_action'),
        'belief_snapshot': pred.get('belief_snapshot'),
        'decision_view': pred.get('decision_view') or {},
        'strategy_tag': strategy_tag,
        'strategy_profile': _safe_copy(strategy_profile) if strategy_profile else {},
        'strategy_family': pred.get('strategy_family') or strategy_profile.get('strategy_family'),
        'strategy_name': pred.get('strategy_name') or strategy_profile.get('strategy_name'),
        'strategy_stage': pred.get('strategy_stage') or strategy_profile.get('stage'),
        'buy_strategy': pred.get('buy_strategy'),
        'strategy_reason': pred.get('strategy_reason'),
        'risk_flags': risk_flags,
        'sample_type': sample_type,
        'task_class': {'buy': 'buy', 'avoid': 'avoid'}.get(bp['category'], 'watch'),
        'error_type': None,
        'root_cause': None,
        'black_swan_flag': False,
        'source': 'local',
        'status': 'open',
        'grade': None,
        'grade_detail': None,
        'retrospective': None,
        'kline_at_close': None,
        'closed_at': None,
    }

    tasks.append(task)
    save_tasks(tasks)
    append_tracking_event(
        'task_created',
        task_id=task['id'],
        code=code,
        name=name,
        source=task.get('source', 'local'),
        message='创建追踪任务',
        payload={
            'prediction_version': pred.get('prediction_version') or pred.get('version'),
            'opportunity_level': pred.get('opportunity_level') or pred.get('final_rating'),
            'strategy_tag': strategy_tag,
            'strategy_family': task.get('strategy_family'),
            'strategy_name': task.get('strategy_name'),
            'strategy_stage': task.get('strategy_stage'),
            'buy_strategy': task.get('buy_strategy'),
            'sample_type': sample_type,
            'kind': task.get('kind'),
            'direction': task.get('direction'),
            'entry_price': task.get('entry_price'),
            'target_pct': task.get('target_pct'),
            'stop_pct': task.get('stop_pct'),
            'final_action': task.get('final_action'),
        },
    )
    logger.info('[tracking] 创建追踪任务 %s %s deadline=%s sample=%s', task['id'], name, deadline, sample_type)
    return task


# ─────────────────────────────────────────────
# 结算
# ─────────────────────────────────────────────

def pending_settle_ids(tasks: list[dict], today: date | None = None) -> list[str]:
    today = today or date.today()
    out: list[str] = []
    for t in tasks:
        if t.get('status') != 'open':
            continue
        dl = str(t.get('deadline') or '')
        try:
            if date.fromisoformat(dl[:10]) <= today:
                out.append(str(t.get('id') or ''))
        except ValueError:
            continue
    return out


def _setup_levels(task: dict) -> tuple[float, float, None] | None:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    if bp.get('category') != 'bullish_watch':
        return None
    entry = bp.get('entry_trigger')
    fail = bp.get('fail_level')
    if entry and fail:
        return float(entry), float(fail), None
    task['conversion_skip_reason'] = 'missing_blueprint_levels'
    return None


def _opportunity_level(task: dict) -> str:
    def _pick(value: Any) -> str:
        text = str(value or '').strip().upper()
        if not text:
            return ''
        match = re.search(r'\b([SABCD])\b', text)
        if match:
            return match.group(1)
        return text[:1]

    for key in ('opportunity_level', 'opportunity_grade', 'setup_level'):
        value = str(task.get(key) or '').strip().upper()
        if value:
            return _pick(value)
    snap = task.get('pred_snapshot') or {}
    for key in ('opportunity_level', 'opportunity_grade', 'setup_level', 'opportunity_label'):
        value = str(snap.get(key) or '').strip().upper()
        if value:
            return _pick(value)
    profile = snap.get('_opportunity_profile') or {}
    if isinstance(profile, dict):
        for key in ('opportunity_level', 'opportunity_grade', 'opportunity_label'):
            value = str(profile.get(key) or '').strip().upper()
            if value:
                return _pick(value)
    return ''


def _is_convertible_watch(task: dict) -> bool:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    return (
        task.get('status') == 'open'
        and bp.get('category') == 'bullish_watch'
        and not bp.get('f_layer_veto')
        and not task.get('converted_from_watch')
        and not task.get('converted_to_task_id')
        and not task.get('conversion_skip_reason')
    )


def _candidate_trigger(task: dict) -> float:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    try:
        return float(bp.get('entry_trigger') or 0)
    except (TypeError, ValueError):
        return 0.0


def _target_price_of(task: dict) -> float:
    try:
        entry = float(task.get('entry_price') or 0)
        tp = float(task.get('target_pct') or 0)
    except (TypeError, ValueError):
        return 0.0
    return entry * (1 + tp / 100) if entry > 0 and tp > 0 else 0.0


def _take_profit_reachable(task: dict, prices: dict | None) -> bool:
    # 进行中 buy 单现价已站上目标价 → 该扫盘中止盈（现价闸门，省 K 线）
    if task.get('status') != 'open' or task.get('task_class') != 'buy':
        return False
    target_price = _target_price_of(task)
    if target_price <= 0:
        return False
    price_obj = (prices or {}).get(str(task.get('code') or ''))
    px = price_obj.get('price') if isinstance(price_obj, dict) else price_obj
    try:
        return px is not None and float(px) >= target_price
    except (TypeError, ValueError):
        return False


def _tp_recheck_due(task: dict, today: date) -> bool:
    if (
        not task.get('tp_pending_recheck')
        or task.get('status') != 'closed'
        or task.get('task_class') != 'buy'
    ):
        return False
    deadline = _parse_date_prefix(task.get('deadline'))
    return deadline is not None and deadline <= today


def _buy_scan_start(task: dict, fallback: date) -> date:
    return _parse_date_prefix(task.get('trigger_date') or task.get('created_at')) or fallback


def _period_high_pct(task: dict, df, settle_date: date, entry: float) -> float | None:
    if entry <= 0 or df is None or getattr(df, 'empty', True) or 'high' not in df.columns:
        return None
    scan_start = _buy_scan_start(task, settle_date)
    period = df[(df.index.date >= scan_start) & (df.index.date <= settle_date)]  # type: ignore[attr-defined]
    if period.empty:
        return None
    try:
        return (float(period['high'].astype(float).max()) - entry) / entry * 100
    except Exception:
        return None


def _fetch_daily_kline_for_tracking(code: str, days: int = 120):
    from core.kline_provider import fetch_daily_kline
    return fetch_daily_kline(code, days=days)


def _normalize_kline(df):
    if df is None or df.empty:
        return df
    out = df.copy()
    out.columns = out.columns.str.lower()
    return out


def _kline_for_task(task: dict, days: int, kline_cache: dict | None = None):
    code = str(task.get('code') or '')
    if not kline_cache is None and code in kline_cache:
        return kline_cache[code]
    df = _fetch_daily_kline_for_tracking(code, days=days)
    df = _normalize_kline(df)
    if kline_cache is not None:
        kline_cache[code] = df
    return df


_CONVERSION_VOLUME_RATIO = 1.5
_CONVERSION_VOLUME_LOOKBACK = 5


def _volume_confirms_trigger(df, trigger_ts):
    """触发日成交量 ≥ 前 N 日均量 × 阈值 → True（放量确认）；< → False（缩量假突破）；
    样本不足 / 无量数据 → None（放行，不因数据缺失卡转换）。"""
    if 'volume' not in df.columns:
        return None
    try:
        loc = int(df.index.get_loc(trigger_ts))
    except (KeyError, TypeError, ValueError):
        return None
    if loc < _CONVERSION_VOLUME_LOOKBACK:
        return None
    try:
        prev = df['volume'].iloc[loc - _CONVERSION_VOLUME_LOOKBACK:loc].astype(float)
        trig_vol = float(df['volume'].iloc[loc])
    except Exception:
        return None
    avg = prev.mean()
    if not avg or avg <= 0:
        return None
    return bool(trig_vol >= avg * _CONVERSION_VOLUME_RATIO)


def _conversion_wait_signature(task: dict) -> str:
    try:
        return json.dumps(task.get('conversion_wait_reason'), sort_keys=True, ensure_ascii=False)
    except Exception:
        return str(task.get('conversion_wait_reason'))


def _set_conversion_wait_reason(task: dict, reason_type: str, message: str, **payload) -> None:
    reason = {'type': reason_type, 'message': message}
    reason.update({k: v for k, v in payload.items() if v is not None})
    task['conversion_wait_reason'] = reason


def _clear_conversion_wait_reason(task: dict) -> None:
    task.pop('conversion_wait_reason', None)


def _trigger_volume_context(df, trigger_ts) -> dict:
    if df is None or df.empty or 'volume' not in df.columns:
        return {}
    try:
        loc = int(df.index.get_loc(trigger_ts))
    except (KeyError, TypeError, ValueError):
        return {}
    if loc < _CONVERSION_VOLUME_LOOKBACK:
        return {}
    try:
        prev = df['volume'].iloc[loc - _CONVERSION_VOLUME_LOOKBACK:loc].astype(float)
        trig_vol = float(df['volume'].iloc[loc])
    except Exception:
        return {}
    avg = float(prev.mean())
    if avg <= 0:
        return {}
    return {
        'volume': round(trig_vol, 3),
        'prev_volume_avg': round(avg, 3),
        'volume_ratio': round(trig_vol / avg, 3),
        'required_ratio': _CONVERSION_VOLUME_RATIO,
    }


def _trigger_rows(task: dict, df, today: date):
    levels = _setup_levels(task)
    if not levels or df is None or df.empty:
        return None
    entry_trigger, fail_level, target_level = levels
    created = _parse_date_prefix(task.get('created_at'))
    if created is None:
        task['conversion_skip_reason'] = 'missing_created_at'
        return None
    df = df.sort_index()
    # 严格从创建日的下一交易日起扫描：预测当日的盘中行情发生在预测之前，不构成触发；
    # 上界截止到原审判日：审判日之后的触发不构成转换，不得延长周期
    deadline = _parse_date_prefix(task.get('deadline'))
    scan_end = min(today, deadline) if deadline else today
    scan = df[(df.index.date > created) & (df.index.date <= scan_end)]  # type: ignore[attr-defined]
    if scan.empty or 'high' not in scan.columns:
        _set_conversion_wait_reason(task, 'kline_missing', 'K线缺失，无法确认', entry_trigger=entry_trigger)
        return None
    hits = scan[scan['high'].astype(float) >= entry_trigger]
    if hits.empty:
        _set_conversion_wait_reason(task, 'price_not_triggered', '未到触发价', entry_trigger=entry_trigger)
        return None
    # 放量确认：碰触发价不等于有效突破，需触发日放量；缩量假突破跳过，继续找后续放量日。
    # 缩量是当天临时状态（次日可能放量），不写 conversion_skip_reason（那是永久跳过标记），
    # 直接当天不转换、次日重扫，避免一次假突破永久打死 2-5 天窗口内的候选。
    for _, row in hits.iterrows():
        if _volume_confirms_trigger(df, row.name) is False:
            _set_conversion_wait_reason(
                task,
                'volume_unconfirmed',
                '触价但缩量，等待放量确认',
                date=row.name.date().isoformat(),
                high=round(float(row.get('high', 0) or 0), 3),
                entry_trigger=entry_trigger,
                **_trigger_volume_context(df, row.name),
            )
            continue
        _clear_conversion_wait_reason(task)
        return row.name.date(), row, entry_trigger, fail_level, target_level
    return None


def _conversion_deadline(trigger_date: date, horizon_days: int) -> str:
    from core.prediction_policy import normalize_horizon_days
    horizon = normalize_horizon_days(horizon_days)
    try:
        from core.trade_calendar import add_trading_days
        return add_trading_days(horizon, start=trigger_date)
    except Exception:
        return (trigger_date + timedelta(days=horizon)).isoformat()


def _apply_inplace_conversion(
    task: dict,
    *,
    trigger_date: date,
    row,
    entry_trigger: float,
    fail_level: float,
    target_level: float,
    origin: str,
) -> dict:
    """原候选单原地变身为买入验证单（不新建任务，便于用户在同一行看到）。"""
    _clear_conversion_wait_reason(task)
    open_price = float(row.get('open'))
    low_price = float(row.get('low'))
    high_price = float(row.get('high'))
    close_price = float(row.get('close'))
    conversion_price = max(entry_trigger, open_price)
    trigger_day_failed = close_price <= fail_level
    now = datetime.now().isoformat(timespec='seconds')
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    ttp = float(bp.get('triggered_target_pct', 0) or 0)
    target_price = entry_trigger * (1 + ttp / 100)
    target_pct = (target_price - conversion_price) / conversion_price * 100
    stop_pct = (fail_level - conversion_price) / conversion_price * 100
    from core.prediction_policy import normalize_horizon_days
    horizon = normalize_horizon_days(task.get('horizon_days'))
    task.setdefault('watch_origin', {
        'created_at': task.get('created_at'),
        'deadline': task.get('deadline'),
        'opportunity_level': _opportunity_level(task),
        'entry_trigger': entry_trigger,
        'fail_level': fail_level,
        'target_level': target_level,
        'horizon_days': horizon,
    })
    pred = task.get('pred_snapshot') or {}
    if isinstance(pred, dict):
        pred['final_action'] = 'buy'
        pred['entry_price'] = conversion_price
        pred['entry_ref'] = conversion_price
        pred['target_pct'] = target_pct
        pred['stop_pct'] = stop_pct
        pred['origin'] = origin
    # 沿用原候选审判日（预测时定的固定周期）；触发当日即破失效价则当日结算
    original_deadline = str((task.get('watch_origin') or {}).get('deadline') or task.get('deadline') or '')[:10]
    if trigger_day_failed:
        new_deadline = trigger_date.isoformat()
    elif original_deadline and original_deadline >= trigger_date.isoformat():
        new_deadline = original_deadline
    else:
        new_deadline = _conversion_deadline(trigger_date, horizon)
    task.update({
        'status': 'open',
        'closed_at': None,
        'grade': None,
        'grade_detail': None,
        'retrospective': None,
        'kline_at_close': None,
        'entry_price': conversion_price,
        'target_pct': target_pct,
        'stop_pct': stop_pct,
        'deadline': new_deadline,
        'final_action': 'buy',
        'task_class': 'buy',
        'sample_type': 'watch',
        'converted_from_watch': True,
        'conversion_status': 'converted',
        'conversion_origin': origin,
        'conversion_price': conversion_price,
        'conversion_at': now,
        'trigger_date': trigger_date.isoformat(),
        'trigger_day_failed': trigger_day_failed,
        'fill_suspect': bool(open_price == high_price == low_price and open_price >= entry_trigger),
    })
    return task


def convert_triggered_watch_tasks(
    tasks: list[dict],
    kline_cache: dict,
    *,
    today: date | None = None,
    origin: str = 'conversion',
    max_codes: int | None = None,
) -> list[dict]:
    today = today or date.today()
    converted: list[dict] = []
    processed_codes: set[str] = set()
    for task in list(tasks):
        if not _is_convertible_watch(task):
            continue
        code = str(task.get('code') or '')
        if max_codes is not None and code not in processed_codes and len(processed_codes) >= max_codes:
            continue
        df = kline_cache.get(code)
        if df is None:
            continue
        processed_codes.add(code)
        df = _normalize_kline(df)
        hit = _trigger_rows(task, df, today)
        if not hit:
            continue
        trigger_date, row, entry_trigger, fail_level, target_level = hit
        _apply_inplace_conversion(
            task,
            trigger_date=trigger_date,
            row=row,
            entry_trigger=entry_trigger,
            fail_level=fail_level,
            target_level=target_level,
            origin=origin,
        )
        converted.append(task)
    return converted


def backfill_triggered_watch_tasks(
    tasks: list[dict],
    kline_cache: dict,
    *,
    today: date | None = None,
) -> list[dict]:
    today = today or date.today()
    backfilled: list[dict] = []
    for task in list(tasks):
        bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
        if (
            task.get('converted_from_watch')
            or task.get('status') != 'closed'
            or bp.get('category') != 'bullish_watch'
        ):
            continue
        code = str(task.get('code') or '')
        df = _normalize_kline(kline_cache.get(code))
        hit = _trigger_rows(task, df, today)
        if not hit:
            continue
        trigger_date, row, entry_trigger, fail_level, target_level = hit
        _apply_inplace_conversion(
            task,
            trigger_date=trigger_date,
            row=row,
            entry_trigger=entry_trigger,
            fail_level=fail_level,
            target_level=target_level,
            origin='backfill',
        )
        backfilled.append(task)
    return backfilled


def _maintenance_codes(
    tasks: list[dict],
    today: date,
    only_ids: set | None = None,
    current_prices: dict | None = None,
) -> list[str]:
    codes: list[str] = []
    for task in tasks:
        if task.get('kind') == 'sector':
            continue
        task_id = str(task.get('id') or '')
        code = str(task.get('code') or '')
        due = task_id in pending_settle_ids([task], today)
        convertible = _is_convertible_watch(task)
        early_tp = (
            task.get('status') == 'open' and task.get('task_class') == 'buy'
            and _target_price_of(task) > 0
        )
        recheck = _tp_recheck_due(task, today)
        if only_ids is not None and task_id not in only_ids:
            due = False
            convertible = False
            early_tp = False
            recheck = False
        if early_tp and only_ids is None and current_prices is not None:
            early_tp = _take_profit_reachable(task, current_prices)
        if convertible and only_ids is None and current_prices is not None:
            price_obj = (current_prices or {}).get(code)
            px = price_obj.get('price') if isinstance(price_obj, dict) else price_obj
            if px is not None:
                try:
                    convertible = _candidate_trigger(task) > 0 and float(px) >= _candidate_trigger(task)
                except (TypeError, ValueError):
                    convertible = False
        if not (convertible or due or early_tp or recheck):
            continue
        if code and code not in codes:
            codes.append(code)
    return codes


def pending_maintenance_ids(
    tasks: list[dict],
    *,
    today: date | None = None,
    attempted: set[str] | None = None,
    current_prices: dict | None = None,
    skip_codes: set[str] | None = None,
    batch_code_limit: int = 8,
) -> list[str]:
    today = today or date.today()
    attempted = attempted or set()
    skip_codes = skip_codes or set()
    prices = current_prices or {}
    ids: list[str] = []
    codes: set[str] = set()
    for task in tasks:
        task_id = str(task.get('id') or '')
        if not task_id or task.get('kind') == 'sector':
            continue
        recheck = _tp_recheck_due(task, today)
        if task_id in attempted and not recheck:
            continue
        code = str(task.get('code') or '')
        due = task_id in pending_settle_ids([task], today)
        convertible = _is_convertible_watch(task)
        # 实时价闸门：现价未到触发价的候选不进批次，省掉 K 线拉取与扫描
        if convertible and current_prices is not None:
            trig = _candidate_trigger(task)
            price_obj = prices.get(code)
            px = price_obj.get('price') if isinstance(price_obj, dict) else price_obj
            try:
                reached = trig > 0 and px is not None and float(px) >= trig
            except (TypeError, ValueError):
                reached = False
            if not reached or code in skip_codes:
                convertible = False
        early_tp = (
            task.get('status') == 'open' and task.get('task_class') == 'buy'
            and _target_price_of(task) > 0
            and code not in skip_codes
            and (current_prices is None or _take_profit_reachable(task, prices))
        )
        if not (convertible or due or early_tp or recheck):
            continue
        if code not in codes and len(codes) >= batch_code_limit:
            continue
        codes.add(code)
        ids.append(task_id)
    return ids


def run_tracking_maintenance(
    tasks: list[dict],
    current_prices: dict,
    *,
    only_ids: set | None = None,
    today: date | None = None,
    batch_code_limit: int = 8,
) -> dict:
    today = today or date.today()
    benchmark_index_df()
    codes = _maintenance_codes(
        tasks,
        today,
        only_ids=only_ids,
        current_prices=current_prices,
    )[:batch_code_limit]
    code_days: dict[str, int] = {code: 120 for code in codes}
    selected_ids: set[str] = set()
    for task in tasks:
        code = str(task.get('code') or '')
        if code not in code_days:
            continue
        task_id = str(task.get('id') or '')
        due = task_id in pending_settle_ids([task], today)
        convertible = _is_convertible_watch(task)
        early_tp = (task.get('status') == 'open' and task.get('task_class') == 'buy'
                    and _target_price_of(task) > 0)
        recheck = _tp_recheck_due(task, today)
        if only_ids is not None and task_id not in only_ids:
            due = False
            convertible = False
            early_tp = False
            recheck = False
        if early_tp and only_ids is None and current_prices is not None:
            early_tp = _take_profit_reachable(task, current_prices)
        if not (due or convertible or early_tp or recheck):
            continue
        selected_ids.add(task_id)
        created = _parse_date_prefix(task.get('created_at'))
        if created is None:
            continue
        code_days[code] = max(code_days[code], (today - created).days + 10, 30)
    kline_cache: dict[str, Any] = {}

    def _fetch_kline_cached(code: str):
        try:
            return code, _normalize_kline(_fetch_daily_kline_for_tracking(code, days=code_days[code]))
        except Exception as e:
            logger.warning('[tracking] 获取 K 线失败 %s: %s', code, e)
            return code, None
    if codes:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(codes))) as ex:
            for c, df_cached in ex.map(_fetch_kline_cached, codes):
                kline_cache[c] = df_cached
    wait_before = {
        str(t.get('id') or ''): _conversion_wait_signature(t)
        for t in tasks
        if _is_convertible_watch(t)
    }
    converted = convert_triggered_watch_tasks(
        tasks,
        kline_cache,
        today=today,
        origin='conversion',
        max_codes=batch_code_limit,
    )
    has_conversion_skip = any(
        t.get('conversion_skip_reason')
        and str(t.get('code') or '') in kline_cache
        for t in tasks
    )
    conversion_waiting = [
        t for t in tasks
        if _is_convertible_watch(t)
        and t.get('conversion_wait_reason')
        and str(t.get('code') or '') in kline_cache
    ]
    has_conversion_wait = any(
        _conversion_wait_signature(t) != wait_before.get(str(t.get('id') or ''))
        for t in conversion_waiting
    )
    settle_ids = only_ids if only_ids is not None else selected_ids
    if only_ids is not None and converted:
        settle_ids = set(only_ids) | {str(t.get('id') or '') for t in converted}
    elif converted:
        settle_ids = set(settle_ids) | {str(t.get('id') or '') for t in converted}
    recheck_ids_before = {
        str(t.get('id') or '') for t in tasks if t.get('tp_pending_recheck')
    }
    settled = try_settle_due_tasks(
        tasks,
        current_prices,
        only_ids=settle_ids,
        kline_cache=kline_cache,
        today=today,
    )
    recheck_dropped = any(
        str(t.get('id') or '') in recheck_ids_before and not t.get('tp_pending_recheck')
        for t in tasks
    )
    if (converted or has_conversion_skip or has_conversion_wait) and not settled:
        save_tasks_merge(tasks)
    return {
        'converted': converted,
        'settled': settled,
        'conversion_waiting': len(conversion_waiting),
        'conversion_wait_changed': has_conversion_wait,
        'recheck_dropped': recheck_dropped,
        'processed_codes': len(codes),
        'pending_codes': max(0, len(_maintenance_codes(
            tasks,
            today,
            only_ids=only_ids,
            current_prices=current_prices,
        )) - len(codes)),
    }


def try_settle_due_tasks(
    tasks: list[dict],
    current_prices: dict,
    only_ids: set | None = None,
    kline_cache: dict | None = None,
    today: date | None = None,
) -> list[dict]:
    """检查已到审判日的 open 任务，用 K 线数据评分并结算。

    Args:
        tasks: 当前全量任务列表（in-place 修改）
        current_prices: {code: {'price': float}} 当前价格快照

    Returns:
        本次新结算的任务列表（可能为空）
    """
    today = today or date.today()
    newly_settled: list[dict] = []
    recheck_dropped = False

    for task in tasks:
        if only_ids is not None and str(task.get('id') or '') not in only_ids:
            continue
        is_recheck = _tp_recheck_due(task, today)
        if task.get('status') != 'open' and not is_recheck:
            continue

        deadline_str = task.get('deadline', '')
        if not deadline_str:
            continue

        try:
            deadline_date = date.fromisoformat(deadline_str)
        except ValueError:
            continue

        is_due = deadline_date <= today
        early_tp = (
            not is_due
            and task.get('kind') != 'sector'
            and task.get('task_class') == 'buy'
            and _target_price_of(task) > 0
        )
        if not is_due and not early_tp and not is_recheck:
            continue

        # 到期 / 进行中已触及目标价：取 K 线评分
        code = task.get('code', '')
        is_sector = task.get('kind') == 'sector'
        try:
            created_date = date.fromisoformat(task['created_at'][:10])
            span_days = (today - created_date).days + 10
            if is_sector:
                from core.kline_provider import fetch_sector_kline
                df = fetch_sector_kline(task.get('name', code), days=max(span_days, 60))
            else:
                df = _kline_for_task(task, max(span_days, 30), kline_cache)
        except Exception as e:
            logger.warning('[tracking] 获取 K 线失败 %s: %s，跳过结算', code, e)
            append_tracking_event(
                'data_anomaly',
                task_id=task.get('id', ''),
                code=code,
                name=task.get('name', ''),
                source=task.get('source', 'local'),
                severity='warning',
                message='获取 K 线失败，跳过结算',
                payload={'error': str(e), 'stage': 'settlement'},
            )
            continue

        if df is None or df.empty:
            logger.warning('[tracking] K 线为空 %s，跳过结算', code)
            append_tracking_event(
                'data_anomaly',
                task_id=task.get('id', ''),
                code=code,
                name=task.get('name', ''),
                source=task.get('source', 'local'),
                severity='warning',
                message='K 线为空，跳过结算',
                payload={'stage': 'settlement'},
            )
            continue

        # 列名统一为小写
        df = _normalize_kline(df)

        # 板块任务走 αβγδ 评分
        if is_sector:
            grade, grade_detail = score_sector_task(task, df, [])
            if grade is None:
                # 数据无效，检查是否达到 zombie 上限（deadline + 30 天宽限期）
                _ZOMBIE_GRACE = 30
                try:
                    zombie_cutoff = deadline_date + timedelta(days=_ZOMBIE_GRACE)
                    if today > zombie_cutoff:
                        task['status'] = 'closed'
                        task['grade'] = None
                        task['grade_detail'] = {'error': 'data_invalid_abandoned'}
                        task['kline_at_close'] = None
                        task['closed_at'] = datetime.now().isoformat(timespec='seconds')
                        newly_settled.append(task)
                        append_tracking_event(
                            'task_settled',
                            task_id=task.get('id', ''),
                            code=code,
                            name=task.get('name', ''),
                            source=task.get('source', 'local'),
                            severity='warning',
                            message='板块数据持续不可用，按无效数据关闭',
                            payload={'grade': None, 'error': 'data_invalid_abandoned'},
                        )
                        logger.warning('[tracking] 板块 %s 数据持续不可用超 %d 天，弃单', task['id'], _ZOMBIE_GRACE)
                    else:
                        logger.warning('[tracking] 板块 %s 数据无效，跳过结算', task['id'])
                        append_tracking_event(
                            'data_anomaly',
                            task_id=task.get('id', ''),
                            code=code,
                            name=task.get('name', ''),
                            source=task.get('source', 'local'),
                            severity='warning',
                            message='板块数据无效，跳过结算',
                            payload={'stage': 'settlement'},
                        )
                except Exception:
                    logger.warning('[tracking] 板块 %s 数据无效，跳过结算', task['id'])
                continue
            kline_compact = _kline_to_compact(df, created_date, deadline_date)
            task['status'] = 'closed'
            task['grade'] = grade
            task['grade_detail'] = grade_detail
            task['kline_at_close'] = kline_compact
            task['closed_at'] = datetime.now().isoformat(timespec='seconds')
            newly_settled.append(task)
            append_tracking_event(
                'task_settled',
                task_id=task.get('id', ''),
                code=code,
                name=task.get('name', ''),
                source=task.get('source', 'local'),
                message='板块追踪任务结算完成',
                payload={
                    'grade': grade,
                    'final_pct': grade_detail.get('final_pct'),
                    'sample_type': task.get('sample_type'),
                    'strategy_tag': task.get('strategy_tag'),
                    'target_hit': grade in ('α', 'β'),
                },
            )
            logger.info('[tracking] 结算板块 %s %s → 评级 %s', task['id'], task['name'], grade)
            continue

        # 找真正的结算日（到期单=deadline；进行中止盈=今日）
        settle_date = _find_settle_date(today if early_tp else deadline_date, df)
        if settle_date is None:
            logger.warning('[tracking] 找不到结算日 %s deadline=%s', code, deadline_str)
            append_tracking_event(
                'data_anomaly',
                task_id=task.get('id', ''),
                code=code,
                name=task.get('name', ''),
                source=task.get('source', 'local'),
                severity='warning',
                message='找不到结算日，跳过结算',
                payload={'deadline': deadline_str},
            )
            continue

        # 进行中止盈：必须确认建单以来确有 high≥目标价，否则保持 open（不误判止损/到期）
        if early_tp:
            cd = _buy_scan_start(task, settle_date)
            seg = df[(df.index.date >= cd) & (df.index.date <= settle_date)]  # type: ignore[attr-defined]
            if seg.empty or float(seg['high'].max()) < _target_price_of(task):
                continue
        recheck_high_pct = None
        if is_recheck:
            try:
                entry_for_recheck = float(task.get('entry_price', 0) or 0)
                target_pct_for_recheck = float(task.get('target_pct') or 0)
            except (TypeError, ValueError):
                task['tp_pending_recheck'] = False
                task.pop('tp_marked_date', None)
                recheck_dropped = True
                continue
            recheck_high_pct = _period_high_pct(task, df, settle_date, entry_for_recheck)
            if recheck_high_pct is None or recheck_high_pct < target_pct_for_recheck:
                task['tp_pending_recheck'] = False
                task.pop('tp_marked_date', None)
                recheck_dropped = True
                continue

        # 取结算价
        settle_rows = df[df.index.date == settle_date]  # type: ignore[attr-defined]
        if settle_rows.empty:
            continue
        deadline_price = float(settle_rows['close'].iloc[-1])

        # 计算 grade
        grade, grade_detail = score_task(task, df, settle_date, deadline_price)
        if is_recheck:
            if grade_detail.get('trigger') != 'target':
                continue
            grade = 'A'
            grade_detail['grade'] = 'A'
            grade_detail['final_pct'] = round(recheck_high_pct, 2)
            grade_detail['settle_date'] = settle_date.isoformat()

        # 存 K 线（紧凑格式）
        kline_compact = _kline_to_compact(df, created_date, settle_date)

        # 更新任务
        task['status'] = 'closed'
        task['grade'] = grade
        task['grade_detail'] = grade_detail
        task['kline_at_close'] = kline_compact
        task['closed_at'] = datetime.now().isoformat(timespec='seconds')
        if early_tp and grade_detail.get('trigger') == 'target':
            task['tp_pending_recheck'] = True
            task['tp_marked_date'] = settle_date.isoformat()
        if is_recheck:
            task['tp_pending_recheck'] = False
            task.pop('tp_marked_date', None)

        newly_settled.append(task)
        append_tracking_event(
            'task_settled',
            task_id=task.get('id', ''),
            code=code,
            name=task.get('name', ''),
            source=task.get('source', 'local'),
            message='追踪任务结算完成',
            payload={
                'grade': grade,
                'final_pct': grade_detail.get('final_pct'),
                'r_multiple': grade_detail.get('r_multiple'),
                'mae_r': grade_detail.get('mae_r'),
                'mfe_r': grade_detail.get('mfe_r'),
                'capture_ratio': grade_detail.get('capture_ratio'),
                'sample_type': task.get('sample_type'),
                'strategy_tag': task.get('strategy_tag'),
                'target_hit': grade in ('A', 'B'),
            },
        )
        logger.info('[tracking] 结算 %s %s → 评级 %s', task['id'], task['name'], grade)

    if newly_settled or recheck_dropped:
        save_tasks_merge(tasks)

    return newly_settled


def _find_settle_date(deadline: date, df) -> date | None:
    """找 deadline 当天或之前最近的 K 线交易日。"""
    available = sorted(set(r.date() for r in df.index))
    candidates = [d for d in available if d <= deadline]
    return candidates[-1] if candidates else None


# ─────────────────────────────────────────────
# ABCDEF 评分引擎
# ─────────────────────────────────────────────

def _plan_quality(task: dict, entry_price: float, target_pct: float, stop_pct: float) -> dict:
    snap = task.get('pred_snapshot') or {}
    raw_entry = task.get('entry_price') or snap.get('entry_price') or snap.get('entry_ref')
    raw_target = task.get('target_pct') if task.get('target_pct') is not None else snap.get('target_pct')
    raw_stop = task.get('stop_pct') if task.get('stop_pct') is not None else snap.get('stop_pct')
    has_entry = entry_price > 0
    has_target = (_float_or_none(raw_target) or 0) > 0
    has_stop = (_float_or_none(raw_stop) or 0) != 0
    has_entry = has_entry and raw_entry not in (None, '', 0)
    rr = round(target_pct / abs(stop_pct), 2) if (target_pct > 0 and stop_pct != 0) else None
    has_action = bool(task.get('final_action') or snap.get('final_action'))
    has_strategy = bool(
        task.get('strategy_tag')
        or snap.get('strategy_tag')
        or snap.get('setup_type')
        or snap.get('thesis')
        or snap.get('reasoning')
    )
    score = sum([has_entry, has_target, has_stop, bool(rr and rr >= 1), has_action, has_strategy])
    if score >= 5:
        label = '完整'
    elif score >= 3:
        label = '基本完整'
    else:
        label = '不足'
    return {
        'label': label,
        'score': score,
        'has_entry': has_entry,
        'has_target': has_target,
        'has_stop': has_stop,
        'has_action': has_action,
        'has_strategy': has_strategy,
        'risk_reward': rr,
    }


def _sniff_stance(text: str) -> str:
    """旧版字符串快照无结构化 stance 时的关键词兜底。

    注意：仅用于无 stance 字段的历史数据；新数据一律读 raw['stance']。
    先判多/空明确词，再判中性，避免"风险/流出"这类普遍出现的词污染判定。
    """
    low = text.lower()
    if any(w in low for w in ('bullish', '看多', '偏强', '利好')):
        return 'bullish'
    if any(w in low for w in ('bearish', '看空', '偏空')):
        return 'bearish'
    if any(w in low for w in ('neutral', '中性', '观望')):
        return 'neutral'
    return 'unknown'


def _agent_hit_map(task: dict, raw_pct: float) -> dict:
    snap = task.get('pred_snapshot') or {}
    agents = {
        'technical': ('技术 Agent', '_agent_technical'),
        'fundflow': ('资金 Agent', '_agent_fundflow'),
        'event': ('事件 Agent', '_agent_news'),
        'fundamental': ('基本面 Agent', '_agent_company'),
        'market': ('市场环境 Agent', '_agent_macro'),
    }
    result: dict[str, dict] = {}
    for key, (label, field) in agents.items():
        raw = snap.get(field)
        if raw in (None, '', {}):
            result[key] = {'label': label, 'status': '样本不足', 'stance': None, 'confidence': None}
            continue

        # 优先读取 Agent 自带的结构化 stance/confidence（权威），
        # 仅当为旧版字符串快照时才退回关键词嗅探。
        if isinstance(raw, dict):
            stance = raw.get('stance')
            conf = raw.get('confidence')
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = None
        else:
            stance = _sniff_stance(str(raw))
            conf = None  # 旧字符串快照无置信度，按下方逻辑视为已表态

        # confidence=0（数据缺失/无判断能力）或 stance 缺失 → 该 Agent 未表态，
        # 不计命中/误导，避免把"没数据"当成"看错了"。
        if stance not in ('bullish', 'bearish', 'neutral') or (isinstance(raw, dict) and (conf is None or conf <= 0)):
            result[key] = {'label': label, 'status': '样本不足', 'stance': stance, 'confidence': conf}
            continue

        # 1.5% 缓冲：A 股日均噪声约 ±2%，窄于噪声的判定无区分力；
        # 宽于 2% 又会把大量真信号判为"未兑现"。1.5% 是经验折中。
        buf = 1.5
        if stance == 'bullish':
            status = '命中' if raw_pct > buf else ('误导' if raw_pct < -buf else '未兑现')
        elif stance == 'bearish':
            status = '命中' if raw_pct < -buf else ('误导' if raw_pct > buf else '未兑现')
        else:  # neutral
            status = '命中' if abs(raw_pct) <= buf else '误导'
        result[key] = {'label': label, 'status': status, 'stance': stance, 'confidence': conf}
    return result


def _execution_quality(task: dict, grade: str, final_pct: float, peak_pct: float, path_quality: str) -> str:
    if grade in ('α', 'β', 'γ', 'δ'):
        if grade in ('α', 'β') and path_quality == 'P_stressed':
            return '达标但回撤偏大'
        if grade in ('α', 'β', 'γ'):
            if peak_pct > 0 and final_pct / peak_pct < 0.35:
                return '有效但回吐偏多'
            return '计划有效'
        return '计划失效'
    task_class = task.get('task_class')
    if task_class == 'avoid':
        if final_pct <= -1.5:
            return '回避正确'
        if final_pct >= 1.5:
            return '回避失败（踏空）'
        return '回避中性'
    if task_class == 'watch':
        if final_pct > 4.0:
            return '观望错过机会'
        if final_pct <= -1.5:
            return '观望躲过下跌'
        return '观望横盘合理'
    if grade == 'A':
        return '止盈达标'
    if grade == 'F':
        return '止损出局'
    if grade in ('B', 'C'):
        if peak_pct > 0 and final_pct / peak_pct < 0.35:
            return '有效但回吐偏多'
        return '计划有效'
    return '未达标'


def _review_metrics(
    task: dict,
    *,
    grade: str,
    final_pct: float,
    peak_pct: float,
    trough_pct: float,
    target_pct: float,
    stop_pct: float,
    entry_price: float,
    path_quality: str,
) -> dict:
    risk = abs(stop_pct)
    r_multiple = round(final_pct / risk, 2) if risk > 0 else None
    mae_r = round(-abs(trough_pct) / risk, 2) if risk > 0 else None
    mfe_r = round(max(peak_pct, 0.0) / risk, 2) if risk > 0 else None
    capture_ratio = round(final_pct / peak_pct, 2) if peak_pct > 0 else None
    return {
        'r_multiple': r_multiple,
        'mae_r': mae_r,
        'mfe_r': mfe_r,
        'capture_ratio': capture_ratio,
        'plan_quality': _plan_quality(task, entry_price, target_pct, stop_pct),
        'execution_quality': _execution_quality(task, grade, final_pct, peak_pct, path_quality),
        'watch_quality': _execution_quality(task, grade, final_pct, peak_pct, path_quality)
        if task.get('sample_type') == 'watch' else None,
        'agent_hit_map': _agent_hit_map(task, final_pct),
    }


def assess_reflection_data_quality(task: dict) -> dict:
    """评估单笔任务用于 AI 反思的输入数据质量（垃圾判断）。

    逐维度判 ok / weak / missing，返回：
      {'level': 'full'|'degraded', 'dimensions': {dim: {'status','note'}}, 'warnings': [...]}
    任一关键维度 missing 或 >=2 维 weak → degraded。
    """
    d = task.get('grade_detail') or {}
    snap = task.get('pred_snapshot') or {}
    dims: dict[str, dict] = {}

    # 计划维度（复用 plan_quality）
    pq = d.get('plan_quality') or {}
    pq_label = pq.get('label') or '未知'
    if pq_label == '完整':
        dims['plan'] = {'status': 'ok', 'note': '计划要素完整'}
    elif pq_label == '基本完整':
        dims['plan'] = {'status': 'weak', 'note': '计划要素基本完整，部分缺失'}
    else:
        dims['plan'] = {'status': 'missing', 'note': '计划要素不足（入场/目标/止损不全）'}

    # 路径维度
    kline = task.get('kline_at_close') or []
    final_pct = float(d.get('final_pct', 0) or 0)
    peak_pct = float(d.get('peak_pct', 0) or 0)
    trough_pct = float(d.get('trough_pct', 0) or 0)
    path_ok_flag = d.get('path_data_ok')
    self_consistent = (peak_pct + 0.5) >= final_pct >= (trough_pct - 0.5)
    degenerate = abs(final_pct) > 1 and abs(peak_pct) < 1e-6 and abs(trough_pct) < 1e-6
    if path_ok_flag is False or not kline or degenerate:
        dims['path'] = {'status': 'missing', 'note': '缺少结算 K 线或回撤数据异常（peak/trough 不可信）'}
    elif not self_consistent:
        dims['path'] = {'status': 'weak', 'note': '回撤数据与最终涨跌不自洽'}
    else:
        dims['path'] = {'status': 'ok', 'note': '路径数据完整自洽'}

    # Agent 维度
    agent_fields = ('_agent_macro', '_agent_company', '_agent_technical',
                    '_agent_fundflow', '_agent_news')
    n_agents = sum(1 for f in agent_fields if snap.get(f) not in (None, '', {}))
    if n_agents >= 4:
        dims['agents'] = {'status': 'ok', 'note': f'{n_agents}/5 Agent 观点存档'}
    elif n_agents >= 2:
        dims['agents'] = {'status': 'weak', 'note': f'仅 {n_agents}/5 Agent 观点存档'}
    else:
        dims['agents'] = {'status': 'missing', 'note': f'Agent 观点存档不足（{n_agents}/5），无法做 Agent 归因'}

    # 环境维度（情报/资金流/情绪，缺失只算 weak，不阻断主归因）
    has_intel = bool(task.get('intel_snapshot'))
    if has_intel:
        dims['context'] = {'status': 'ok', 'note': '有情报快照'}
    else:
        dims['context'] = {'status': 'weak', 'note': '无情报快照，情绪/资金面归因谨慎'}

    statuses = [v['status'] for v in dims.values()]
    n_missing = statuses.count('missing')
    n_weak = statuses.count('weak')
    level = 'degraded' if (n_missing >= 1 or n_weak >= 2) else 'full'
    warnings = [f"{k}: {v['note']}" for k, v in dims.items() if v['status'] != 'ok']
    return {'level': level, 'dimensions': dims, 'warnings': warnings}


def _signal_ref_price(task: dict, df) -> float:
    snap = task.get('pred_snapshot') or {}
    for v in (task.get('entry_price'), task.get('entry_ref'),
              snap.get('entry_ref'), snap.get('entry_price'),
              snap.get('current_price'), snap.get('close')):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    if df is not None and not getattr(df, 'empty', True):
        try:
            created = date.fromisoformat(str(task.get('created_at', ''))[:10])
            period = df[df.index.date >= created]  # type: ignore
            if not period.empty:
                return float(period['close'].iloc[0])
        except Exception:
            pass
    return 0.0


def _path_extremes(task: dict, df, settle_date: date, entry: float):
    try:
        created_date = date.fromisoformat(str(task.get('created_at', ''))[:10])
    except Exception:
        created_date = settle_date
    if df is not None and not getattr(df, 'empty', True) and entry > 0:
        try:
            mask = (df.index.date >= created_date) & (df.index.date <= settle_date)  # type: ignore
            period = df[mask]
            if not period.empty:
                peak = (float(period['high'].max()) - entry) / entry * 100
                trough = (float(period['low'].min()) - entry) / entry * 100
                return peak, trough, True
        except Exception:
            pass
    kline = task.get('kline_at_close') or []
    highs = [float(r.get('h', 0) or 0) for r in kline if float(r.get('h', 0) or 0) > 0]
    lows = [float(r.get('l', 0) or 0) for r in kline if float(r.get('l', 0) or 0) > 0]
    if highs and lows and entry > 0:
        peak = (max(highs) - entry) / entry * 100
        trough = (min(lows) - entry) / entry * 100
        return peak, trough, True
    return None, None, False


def _grade_watch(raw):
    # 中性观望对散户：不涨即正确，只惩罚踏空。涨 >4% 算 D，>6%(4%的1.5倍)算 F，其余全 A
    if raw > 6.0:
        return 'F', 'watch_miss_big'
    if raw > 4.0:
        return 'D', 'watch_miss'
    if raw <= -1.5:
        return 'A', 'watch_dodge'
    return 'A', 'watch_flat'


def _grade_no_position(raw):
    if raw <= -5:
        return 'A', 'fall_big'
    if raw <= -2:
        return 'B', 'fall_small'
    if raw < 2:
        return 'C', 'flat'
    if raw < 5:
        return 'D', 'rise_small'
    return 'F', 'rise_big'


def _grade_buy_expire(raw, has_plan, target_pct):
    if has_plan:
        if raw >= target_pct * 0.5:
            return 'B'
        if raw >= 0:
            return 'C'
        return 'D'
    if raw >= 5:
        return 'A'
    if raw >= 2:
        return 'B'
    if raw >= 0:
        return 'C'
    if raw >= -5:
        return 'D'
    return 'F'


def _score_no_position(task: dict, df, settle_date: date, deadline_price: float, task_class: str) -> tuple[str, dict]:
    entry = float(task.get('entry_price', 0) or 0)
    if entry <= 0:
        entry = _signal_ref_price(task, df)
    if entry <= 0:
        return '', {'error': 'entry_price 无效', 'grade': ''}
    raw = (deadline_price - entry) / entry * 100
    expected = task.get('expected_range_pct') or (task.get('pred_snapshot') or {}).get('expected_range_pct')
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    avoid_target = float(bp.get('target_pct', 0) or 0)
    avoid_fail = float(bp.get('fail_level', 0) or 0)
    if bp.get('category') == 'avoid' and avoid_target > 0 and avoid_fail > 0:
        grade = tier = None
        target_down = entry * (1 - avoid_target / 100)
        # 盘中扫描：盘中跌达目标跌幅价即兑现（目标优先），止损=收盘站上失效价才算破位（盘中插针收回不算）；
        # 先到先算、同日目标优先；都没碰 → 收盘兜底。从建单次日起扫（建单当天盘中在预测前）
        if df is not None and not getattr(df, 'empty', True):
            try:
                created = date.fromisoformat(str(task.get('created_at', ''))[:10])
            except Exception:
                created = settle_date
            try:
                mask = (df.index.date > created) & (df.index.date <= settle_date)  # type: ignore[attr-defined]
                broke = dropped = False
                drop_date = None
                for _ts, _row in df[mask].sort_index().iterrows():
                    if float(_row['low']) <= target_down:
                        dropped = True
                        drop_date = _ts.date()
                        break
                    if float(_row['close']) >= avoid_fail:
                        broke = True
                        break
                if dropped:
                    grade, tier = 'A', 'drop_target'
                    settle_date = drop_date or settle_date
                    deadline_price = target_down
                    raw = -avoid_target
                elif broke:
                    grade, tier = 'F', 'breakout_intraday'
            except Exception:
                grade = tier = None
        if grade is None:
            if deadline_price >= avoid_fail:
                grade, tier = 'F', 'breakout_missed'
            elif raw <= -avoid_target:
                grade, tier = 'A', 'drop_target'
            elif raw <= -avoid_target * 0.5:
                grade, tier = 'B', 'drop_half'
            elif raw < 2.0:
                grade, tier = 'C', 'flat'
            else:
                grade, tier = 'D', 'rise_missed'
    elif task_class == 'watch':
        grade, tier = _grade_watch(raw)
    else:
        grade, tier = _grade_no_position(raw)
    peak_pct, trough_pct, path_data_ok = _path_extremes(task, df, settle_date, entry)
    if peak_pct is None:
        peak_pct, trough_pct = max(raw, 0.0), min(raw, 0.0)
    detail = {
        'grade': grade,
        'task_class': task_class,
        'outcome_tier': tier,
        'final_pct': round(raw, 2),
        'peak_pct': round(peak_pct, 2),
        'trough_pct': round(trough_pct, 2),
        'path_data_ok': path_data_ok,
        'path_quality': 'neutral',
        'expected_range_pct': expected,
        'entry_price': entry,
        'deadline_price': deadline_price,
        'direction': str(task.get('direction', '')),
        'settle_date': settle_date.isoformat(),
    }
    detail.update(_review_metrics(
        task,
        grade=grade,
        final_pct=raw,
        peak_pct=peak_pct,
        trough_pct=trough_pct,
        target_pct=0.0,
        stop_pct=0.0,
        entry_price=entry,
        path_quality='no_position',
    ))
    return grade, detail


def _score_buy_task(task: dict, df, settle_date: date, deadline_price: float) -> tuple[str, dict]:
    entry = float(task.get('entry_price', 0) or 0)
    target_pct = float(task.get('target_pct') or 0)
    stop_pct = float(task.get('stop_pct') or 0)
    if entry <= 0:
        return '', {'error': 'entry_price 无效', 'grade': ''}
    has_plan = target_pct > 0 and stop_pct != 0
    target_price = entry * (1 + target_pct / 100)
    # 统一止损线（仅审判日结算判定，与买入盈亏比解耦）：ETF 5% / 个股 10%
    fixed_stop_pct = 5.0 if _is_etf_task(task) else 10.0
    stop_price = entry * (1 - fixed_stop_pct / 100)
    try:
        scan_start = task.get('trigger_date') or task['created_at']
        created_date = date.fromisoformat(str(scan_start)[:10])
    except Exception:
        created_date = settle_date
    grade = None
    trigger = 'expire'
    outcome_pct = None
    effective_settle = settle_date
    scan_target = target_pct > 0
    # 止盈：过程中（含审判日）盘中触及目标价即兑现，先到先得
    if scan_target and df is not None and not df.empty:
        mask = (df.index.date >= created_date) & (df.index.date <= settle_date)  # type: ignore
        period = df[mask].sort_index()
        try:
            period_high = float(period['high'].astype(float).max()) if not period.empty else None
        except Exception:
            period_high = None
        for ts, row in period.iterrows():
            high = float(row['high'])
            if high >= target_price:
                high_pct = ((period_high or target_price) - entry) / entry * 100
                grade, trigger, outcome_pct = 'A', 'target', high_pct
                effective_settle = ts.date()
                break
    # 止损：过程中碰到不算，只看审判日收盘是否仍跌破统一止损线
    if grade is None:
        raw = (deadline_price - entry) / entry * 100
        outcome_pct = raw
        if deadline_price <= stop_price:
            grade, trigger = 'F', 'stop'
        else:
            grade = _grade_buy_expire(raw, has_plan, target_pct)
    if trigger == 'target':
        outcome_tier = 'T_target'
    elif trigger in ('stop', 'stop_same_day'):
        outcome_tier = 'T_stop'
    elif has_plan:
        outcome_tier = {'B': 'T_expire_half', 'C': 'T_expire_gain', 'D': 'T_expire_loss'}.get(grade, 'T_expire')
    else:
        outcome_tier = {'A': 'bull_big_gain', 'B': 'bull_gain', 'C': 'bull_flat', 'D': 'bull_small_loss', 'F': 'bull_big_loss'}.get(grade, 'bull')
    peak_pct, trough_pct, path_data_ok = _path_extremes(task, df, effective_settle, entry)
    if peak_pct is None:
        peak_pct, trough_pct = max(outcome_pct, 0.0), min(outcome_pct, 0.0)
    abs_stop = fixed_stop_pct
    if abs(trough_pct) < abs_stop * 0.5:
        path_quality = 'P_clean'
    elif abs(trough_pct) < abs_stop * 0.9:
        path_quality = 'P_tested'
    else:
        path_quality = 'P_stressed'
    detail = {
        'grade': grade,
        'task_class': 'buy',
        'outcome_tier': outcome_tier,
        'trigger': trigger,
        'final_pct': round(outcome_pct, 2),
        'peak_pct': round(peak_pct, 2),
        'trough_pct': round(trough_pct, 2),
        'path_data_ok': path_data_ok,
        'path_quality': path_quality,
        'target_pct': target_pct,
        'stop_pct': stop_pct,
        'entry_price': entry,
        'deadline_price': deadline_price,
        'direction': str(task.get('direction', 'bullish')),
        'settle_date': effective_settle.isoformat(),
    }
    detail.update(_review_metrics(
        task,
        grade=grade,
        final_pct=outcome_pct,
        peak_pct=peak_pct,
        trough_pct=trough_pct,
        target_pct=target_pct,
        stop_pct=stop_pct,
        entry_price=entry,
        path_quality=path_quality,
    ))
    return grade, detail


def _expire_trigger_volume(task: dict, df, entry_trigger: float, settle_date: date):
    """到期未转换却收盘站上触发价时，回查触发日是否放量。True=放量；False/None=缩量或无法判断。"""
    if df is None or getattr(df, 'empty', True):
        return None
    try:
        created = date.fromisoformat(str(task.get('created_at', ''))[:10])
    except Exception:
        return None
    try:
        d2 = df.sort_index()
        mask = (d2.index.date > created) & (d2.index.date <= settle_date)  # type: ignore[attr-defined]
        scan = d2[mask]
        if scan.empty or 'high' not in scan.columns:
            return None
        hits = scan[scan['high'].astype(float) >= entry_trigger]
        if hits.empty:
            return None
        return _volume_confirms_trigger(d2, hits.iloc[0].name)
    except Exception:
        return None


def _score_bullish_watch_expire(task: dict, df, settle_date: date, deadline_price: float) -> tuple[str, dict]:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    entry = float(task.get('entry_price', 0) or 0)
    entry_trigger = float(bp.get('entry_trigger', 0) or 0)
    fail_level = float(bp.get('fail_level', 0) or 0)
    if entry_trigger <= 0 or fail_level <= 0 or entry <= 0:
        return '', {'error': 'blueprint 缺触发/失效价', 'grade': ''}
    raw = (deadline_price - entry) / entry * 100
    if raw <= -6.0:
        grade, tier = 'D', 'setup_broken'
    elif raw >= 0.0:
        grade, tier = 'B', 'waited_right'
    else:
        grade, tier = 'C', 'waited_flat'
    peak_pct, trough_pct, path_data_ok = _path_extremes(task, df, settle_date, entry)
    if peak_pct is None:
        peak_pct, trough_pct = max(raw, 0.0), min(raw, 0.0)
    detail = {
        'grade': grade,
        'task_class': 'watch',
        'outcome_tier': tier,
        'final_pct': round(raw, 2),
        'peak_pct': round(peak_pct, 2),
        'trough_pct': round(trough_pct, 2),
        'path_data_ok': path_data_ok,
        'path_quality': 'neutral',
        'entry_trigger': entry_trigger,
        'fail_level': fail_level,
        'entry_price': entry,
        'deadline_price': deadline_price,
        'direction': str(task.get('direction', '')),
        'settle_date': settle_date.isoformat(),
    }
    detail.update(_review_metrics(
        task,
        grade=grade,
        final_pct=raw,
        peak_pct=peak_pct,
        trough_pct=trough_pct,
        target_pct=0.0,
        stop_pct=0.0,
        entry_price=entry,
        path_quality='no_position',
    ))
    return grade, detail


def score_task(task: dict, df, settle_date: date, deadline_price: float) -> tuple[str, dict]:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    if bp.get('category') == 'bullish_watch' and not task.get('converted_from_watch') and not bp.get('f_layer_veto'):
        return _score_bullish_watch_expire(task, df, settle_date, deadline_price)
    entry_price = float(task.get('entry_price', 0) or 0)
    task_class = task.get('task_class')
    if task_class == 'buy':
        if entry_price <= 0:
            return '', {'error': 'entry_price 无效', 'grade': ''}
        return _score_buy_task(task, df, settle_date, deadline_price)
    return _score_no_position(task, df, settle_date, deadline_price, task_class)


def recompute_task(task: dict, df) -> bool:
    if df is None or df.empty:
        return False
    df = _normalize_kline(df)
    try:
        deadline_date = date.fromisoformat(str(task.get('deadline') or '')[:10])
    except ValueError:
        return False
    created_date = _parse_date_prefix(task.get('created_at')) or deadline_date
    settle_date = _find_settle_date(deadline_date, df)
    if settle_date is None:
        return False
    settle_rows = df[df.index.date == settle_date]  # type: ignore[attr-defined]
    if settle_rows.empty:
        return False
    deadline_price = float(settle_rows['close'].iloc[-1])
    grade, detail = score_task(task, df, settle_date, deadline_price)
    task['grade'] = grade
    task['grade_detail'] = detail
    task['kline_at_close'] = _kline_to_compact(df, created_date, settle_date)
    task['retrospective'] = None
    task['error_type'] = None
    task['root_cause'] = None
    return True


# ─────────────────────────────────────────────
# 板块追踪评分（αβγδ）
# ─────────────────────────────────────────────

def score_sector_task(task: dict, kline_df, intel_events: list) -> tuple[str, dict]:
    """板块任务评级：αβγδ 四档 + trend_quality + intel_hit。

    α: final_pct >= target_pct
    β: target_pct*0.5 <= final_pct < target_pct
    γ: -abs(stop_pct) <= final_pct < target_pct*0.5   （未跌破止损线）
    δ: final_pct < -abs(stop_pct)（即亏损超过止损阈值）
    """
    from core.utils.mytt import MA

    entry_price = float(task.get('entry_price', 0) or 0)
    target_pct = float(task.get('target_pct', 5.0))
    stop_pct = float(task.get('stop_pct', -2.5))  # 负数
    direction = str(task.get('direction', '多'))
    is_long = direction in ('多', 'bullish', 'long', '看多')

    if entry_price <= 0 or kline_df is None or kline_df.empty:
        return None, {'error': 'data_invalid'}

    df = kline_df.copy()
    df.columns = df.columns.str.lower()

    try:
        created_date = date.fromisoformat(task['created_at'][:10])
        deadline_str = task.get('deadline', task['created_at'][:10])
        settle_date = date.fromisoformat(deadline_str)
    except Exception:
        settle_date = date.today()
        created_date = settle_date

    mask = (df.index.date >= created_date) & (df.index.date <= settle_date)  # type: ignore
    period_df = df[mask]

    if period_df.empty:
        return 'δ', {'error': 'K 线区间为空', 'grade': 'δ'}

    deadline_price = float(period_df['close'].iloc[-1])
    final_pct = (deadline_price - entry_price) / entry_price * 100
    if not is_long:
        final_pct = -final_pct

    # αβγδ 判定
    if final_pct >= target_pct:
        grade = 'α'
    elif final_pct >= target_pct * 0.5:
        grade = 'β'
    elif final_pct >= -abs(stop_pct):   # 未跌破止损线（幅度归一，兼容正负 stop_pct）
        grade = 'γ'
    else:
        grade = 'δ'

    # trend_quality：MA5 > MA20 保持比例
    trend_quality: float | None = None
    try:
        closes = df['close'].values.astype(float)
        ma5 = MA(closes, 5)
        ma20 = MA(closes, 20)
        period_mask_idx = [i for i, d in enumerate(df.index.date) if created_date <= d <= settle_date]  # type: ignore
        if period_mask_idx:
            bulls = sum(1 for i in period_mask_idx if ma5[i] > ma20[i])
            trend_quality = round(bulls / len(period_mask_idx), 3)
    except Exception:
        pass

    # intel_hit：情报方向与预测方向一致的比例
    intel_hit: float | None = None
    try:
        code = task.get('code', '')
        name = task.get('name', '')
        relevant = [
            e for e in intel_events
            if code in (getattr(e, 'category', '') or '')
               or name in (getattr(e, 'title', '') or '')
               or any(name in s for s in (getattr(e, 'related_sectors', []) or []))
        ]
        if relevant:
            match_dir = 'bullish' if is_long else 'bearish'
            hits = sum(1 for e in relevant if getattr(e, 'direction', '') == match_dir)
            intel_hit = round(hits / len(relevant), 3)
    except Exception:
        pass

    if is_long:
        trough_pct = (period_df['low'].min() - entry_price) / entry_price * 100
        peak_pct = (period_df['high'].max() - entry_price) / entry_price * 100
    else:
        high_pct = (period_df['high'].max() - entry_price) / entry_price * 100
        low_pct = (period_df['low'].min() - entry_price) / entry_price * 100
        trough_pct = high_pct
        peak_pct = -low_pct
    path_quality = 'P_clean' if trend_quality is not None and trend_quality >= 0.7 else 'P_stressed'

    detail = {
        'grade': grade,
        'final_pct': round(final_pct, 2),
        'peak_pct': round(peak_pct, 2),
        'trough_pct': round(trough_pct, 2),
        'path_quality': path_quality,
        'target_pct': target_pct,
        'stop_pct': stop_pct,
        'deadline_price': deadline_price,
        'entry_price': entry_price,
        'direction': direction,
        'settle_date': settle_date.isoformat(),
        'trend_quality': trend_quality,
        'intel_hit': intel_hit,
    }
    detail.update(_review_metrics(
        task,
        grade=grade,
        final_pct=final_pct,
        peak_pct=peak_pct,
        trough_pct=trough_pct,
        target_pct=target_pct,
        stop_pct=stop_pct,
        entry_price=entry_price,
        path_quality=path_quality,
    ))
    return grade, detail


# ─────────────────────────────────────────────
# 统计
# ─────────────────────────────────────────────

_BENCHMARK_INDEX_CODE = '000985'
_benchmark_index_cache: dict[str, Any] = {'date': None, 'df': None}


def benchmark_index_df():
    today = date.today().isoformat()
    if _benchmark_index_cache['date'] == today and _benchmark_index_cache['df'] is not None:
        return _benchmark_index_cache['df']
    df = None
    try:
        from core.kline_provider import fetch_index_kline
        raw = fetch_index_kline(_BENCHMARK_INDEX_CODE, days=600)
        if raw is not None and not raw.empty:
            df = raw.copy()
            df.columns = df.columns.str.lower()
    except Exception as e:
        logger.warning('[tracking] 基准指数拉取失败: %s', e)
        df = None
    if df is not None:
        _benchmark_index_cache['date'] = today
        _benchmark_index_cache['df'] = df
    return df


def _index_window_return(index_df, start: date, end: date) -> float | None:
    if index_df is None or index_df.empty or 'close' not in index_df.columns or end < start:
        return None
    base_rows = index_df[index_df.index.date <= start]  # type: ignore[attr-defined]
    last_rows = index_df[index_df.index.date <= end]    # type: ignore[attr-defined]
    if base_rows.empty or last_rows.empty:
        return None
    base = float(base_rows['close'].iloc[-1])
    last = float(last_rows['close'].iloc[-1])
    if base <= 0:
        return None
    return (last / base - 1) * 100


def get_tracking_stats(tasks: list[dict], source: str | None = None, index_df=None) -> dict:
    """计算追踪任务统计数据。

    Args:
        tasks: 任务列表
        source: 按来源过滤（'local'/'imported:xxx'/None=全部）
        index_df: 大盘基准指数日 K（含 close，DatetimeIndex）。传入则按逐样本同期
            alpha 计算 skill_stats；为 None 时回退到内部篮子均值基准。

    Returns:
        {
          total, open_count, closed_count,
          win_count, loss_count,
          win_rate,        # A+B+C / closed（None 时样本不足）
          target_rate,     # A+B / closed（达标率）
          grade_dist,      # {'A': n, 'B': n, 'C': n, 'D': n, 'F': n}
        }
    """
    if source:
        tasks = [t for t in tasks if t.get('source', 'local') == source]
    open_tasks = [t for t in tasks if t.get('status') == 'open']
    closed_tasks = [t for t in tasks if t.get('status') == 'closed']

    # 按 kind 分组（排除旧两段式遗留的原候选父单：其买入样本由关联子单承载）
    stock_closed = [t for t in closed_tasks
                    if t.get('kind', 'stock') != 'sector'
                    and not t.get('converted_to_task_id')]
    sector_closed = [t for t in closed_tasks
                     if t.get('kind') == 'sector'
                     and (t.get('grade_detail') or {}).get('error') != 'data_invalid_abandoned']

    grade_dist: dict[str, int] = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
    sector_grade_dist: dict[str, int] = {'α': 0, 'β': 0, 'γ': 0, 'δ': 0}
    ungraded_closed = 0  # 已关闭但无评级（如导入数据尚未结算）
    by_class = {k: {'total': 0, 'win': 0, 'target': 0} for k in ('buy', 'avoid', 'watch')}
    fill_suspect_stats = {'total': 0, 'win': 0, 'target': 0}
    for t in stock_closed:
        g = t.get('grade')
        if g and g in grade_dist:
            grade_dist[g] += 1
        elif not g:
            ungraded_closed += 1
        else:
            logger.warning('[tracking] 未知 grade 值 "%s" 归入 F 桶: %s %s', g, t.get('code'), t.get('name'))
            grade_dist['F'] += 1
        tc = t.get('task_class')
        if t.get('fill_suspect'):
            fill_suspect_stats['total'] += 1
            if g in ('A', 'B', 'C'):
                fill_suspect_stats['win'] += 1
            if g in ('A', 'B'):
                fill_suspect_stats['target'] += 1
            if tc == 'buy':
                continue
        if tc in by_class and g in ('A', 'B', 'C', 'D', 'F'):
            by_class[tc]['total'] += 1
            if g in ('A', 'B', 'C'):
                by_class[tc]['win'] += 1
            if g in ('A', 'B'):
                by_class[tc]['target'] += 1
    for v in by_class.values():
        v['rate'] = v['win'] / v['total'] if v['total'] else None
        v['target_rate'] = v['target'] / v['total'] if v['total'] else None
    fill_suspect_stats['rate'] = (
        fill_suspect_stats['win'] / fill_suspect_stats['total']
        if fill_suspect_stats['total'] else None
    )
    for t in sector_closed:
        g = t.get('grade')
        if g and g in sector_grade_dist:
            sector_grade_dist[g] += 1
        elif not g:
            ungraded_closed += 1

    closed_count = len(closed_tasks)
    stock_closed_count = len(stock_closed)
    sector_closed_count = len(sector_closed)
    graded_stock_count = stock_closed_count - ungraded_closed

    win_count = grade_dist['A'] + grade_dist['B'] + grade_dist['C']
    target_count = grade_dist['A'] + grade_dist['B']
    loss_count = grade_dist['D'] + grade_dist['F']
    sector_win_count = sector_grade_dist['α'] + sector_grade_dist['β']

    win_rate = win_count / graded_stock_count if graded_stock_count >= 5 else None
    target_rate = target_count / graded_stock_count if graded_stock_count >= 5 else None
    sector_win_rate = sector_win_count / sector_closed_count if sector_closed_count >= 3 else None

    trade_stock_closed = [t for t in stock_closed if t.get('sample_type') == 'trade']
    trade_win_count = sum(1 for t in trade_stock_closed if t.get('grade') in ('A', 'B', 'C'))
    trade_target_count = sum(1 for t in trade_stock_closed if t.get('grade') in ('A', 'B'))
    trade_win_rate = trade_win_count / len(trade_stock_closed) if len(trade_stock_closed) >= 5 else None
    trade_target_rate = trade_target_count / len(trade_stock_closed) if len(trade_stock_closed) >= 5 else None

    strategy_stats: dict[str, dict[str, int]] = {}
    for t in stock_closed:
        tag = str(t.get('strategy_tag') or '未分类')
        row = strategy_stats.setdefault(tag, {'total': 0, 'win': 0, 'target': 0})
        row['total'] += 1
        if t.get('grade') in ('A', 'B', 'C'):
            row['win'] += 1
        if t.get('grade') in ('A', 'B'):
            row['target'] += 1

    use_market = index_df is not None
    skill_units: dict[tuple[str, str], list[float]] = {}
    market_units: dict[tuple[str, str], list[float]] = {}
    for t in stock_closed:
        d = t.get('grade_detail') or {}
        fp = d.get('final_pct')
        if fp is None:
            continue
        cls = t.get('task_class')
        if cls not in ('buy', 'avoid', 'watch'):
            continue
        key = (_norm_code(t.get('code')), cls)
        mkt = None
        if use_market:
            start = _parse_date_prefix(t.get('trigger_date') or t.get('created_at'))
            end = _parse_date_prefix(d.get('settle_date')) or _parse_date_prefix(t.get('deadline'))
            if start is not None and end is not None:
                mkt = _index_window_return(index_df, start, end)
            if mkt is None:
                continue
            skill_units.setdefault(key, []).append(float(fp) - mkt)
            market_units.setdefault(key, []).append(mkt)
        else:
            skill_units.setdefault(key, []).append(float(fp))
    unit_returns = {k: sum(v) / len(v) for k, v in skill_units.items()}
    if use_market:
        market_means = {k: sum(v) / len(v) for k, v in market_units.items()}
        benchmark = sum(market_means.values()) / len(market_means) if market_means else None
    else:
        all_returns = list(unit_returns.values())
        benchmark = sum(all_returns) / len(all_returns) if all_returns else None
    skill_buckets: dict[str, dict] = {}
    bucket_mean: dict[str, float | None] = {}
    for cls in ('buy', 'avoid', 'watch'):
        vals = [r for (code, c), r in unit_returns.items() if c == cls]
        n = len(vals)
        mean = sum(vals) / n if n else None
        bucket_mean[cls] = mean
        if n < 5 or mean is None:
            excess = None
        elif use_market:
            excess = mean
        elif benchmark is not None:
            excess = mean - benchmark
        else:
            excess = None
        skill_buckets[cls] = {'n': n, 'mean': mean, 'excess': excess}
    if skill_buckets['buy']['n'] >= 5 and skill_buckets['avoid']['n'] >= 5:
        spread = bucket_mean['buy'] - bucket_mean['avoid']
    else:
        spread = None
    skill_stats = {
        'benchmark': benchmark,
        'benchmark_kind': 'market' if use_market else 'internal',
        'unit_count': len(unit_returns),
        'buckets': skill_buckets,
        'spread': spread,
    }
    ab_candidates = [
        t for t in tasks
        if t.get('converted_from_watch')
        or ((t.get('pred_snapshot') or {}).get('task_blueprint') or {}).get('category') == 'bullish_watch'
    ]
    converted_ab_candidates = [
        t for t in ab_candidates
        if t.get('converted_from_watch') or t.get('converted_to_task_id')
    ]
    conversion_stats = {
        'total_candidates': len(ab_candidates),
        'converted': len(converted_ab_candidates),
        'rate': (
            len(converted_ab_candidates) / len(ab_candidates)
            if ab_candidates else None
        ),
    }

    # 置信度校准：AI 高置信单是否真更准（低1-4/中5-6/高7-10），用 stock_closed 同口径
    confidence_calibration = {
        'low': {'range': '1-4', 'closed': 0, 'win': 0, 'target': 0},
        'mid': {'range': '5-6', 'closed': 0, 'win': 0, 'target': 0},
        'high': {'range': '7-10', 'closed': 0, 'win': 0, 'target': 0},
    }
    for t in stock_closed:
        g = t.get('grade')
        if g not in ('A', 'B', 'C', 'D', 'F'):
            continue
        try:
            conf = float(t.get('confidence') or 0)
        except (TypeError, ValueError):
            continue
        if conf <= 0:
            continue
        bucket = (
            confidence_calibration['low'] if conf <= 4
            else confidence_calibration['mid'] if conf <= 6
            else confidence_calibration['high']
        )
        bucket['closed'] += 1
        if g in ('A', 'B', 'C'):
            bucket['win'] += 1
        if g in ('A', 'B'):
            bucket['target'] += 1
    for b in confidence_calibration.values():
        b['win_rate'] = b['win'] / b['closed'] if b['closed'] else None
        b['target_rate'] = b['target'] / b['closed'] if b['closed'] else None

    return {
        'total': len(tasks),
        'open_count': len(open_tasks),
        'closed_count': closed_count,
        'ungraded_closed': ungraded_closed,
        'graded_stock_count': graded_stock_count,
        'win_count': win_count,
        'target_count': target_count,
        'loss_count': loss_count,
        'win_rate': win_rate,
        'target_rate': target_rate,
        'grade_dist': grade_dist,
        'by_class': by_class,
        'conversion': conversion_stats,
        'fill_suspect': fill_suspect_stats,
        'sector_win_rate': sector_win_rate,
        'sector_win_count': sector_win_count,
        'sector_closed_count': sector_closed_count,
        'sector_grade_dist': sector_grade_dist,
        'trade_closed_count': len(trade_stock_closed),
        'trade_win_count': trade_win_count,
        'trade_target_count': trade_target_count,
        'trade_win_rate': trade_win_rate,
        'trade_target_rate': trade_target_rate,
        'strategy_stats': strategy_stats,
        'skill_stats': skill_stats,
        'confidence_calibration': confidence_calibration,
    }


# ─────────────────────────────────────────────
# 聚合复盘报告
# ─────────────────────────────────────────────

def _parse_date_prefix(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or '')[:10])
    except Exception:
        return None


def _report_task_date(task: dict) -> date | None:
    return _parse_date_prefix(task.get('closed_at')) or _parse_date_prefix(task.get('created_at'))


def _range_days(value: Any) -> int | None:
    if value in (None, '', 'all', '全部'):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value)
    digits = ''.join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _is_etf_task(task: dict) -> bool:
    kind = str(task.get('kind') or '').lower()
    code = str(task.get('code') or '')
    name = str(task.get('name') or '').upper()
    return kind == 'etf' or 'ETF' in name or code.startswith(('15', '16', '51', '56', '58'))


def _filter_report_tasks(tasks: list[dict], options: dict) -> list[dict]:
    sample_scope = str(options.get('sample_scope') or 'all')
    kind_scope = str(options.get('kind_scope') or 'all')
    days = _range_days(options.get('range_days'))
    cutoff = date.today() - timedelta(days=days) if days else None

    out: list[dict] = []
    for task in tasks:
        task_date = _report_task_date(task)
        if cutoff and (task_date is None or task_date < cutoff):
            continue

        sample = str(task.get('sample_type') or 'watch')
        if sample_scope in ('trade', 'formal') and sample != 'trade':
            continue
        if sample_scope in ('watch', '观察单') and sample != 'watch':
            continue

        kind = str(task.get('kind') or 'stock')
        if kind_scope in ('sector', '板块') and kind != 'sector':
            continue
        if kind_scope in ('etf', 'ETF') and not _is_etf_task(task):
            continue
        if kind_scope in ('stock', '个股') and (kind == 'sector' or _is_etf_task(task)):
            continue
        out.append(task)
    return out


def _detail(task: dict) -> dict:
    return task.get('grade_detail') if isinstance(task.get('grade_detail'), dict) else {}


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == '':
            return None
        return float(value)
    except Exception:
        return None


def _avg(values: list[float | None]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


def _avg_label(values: list[float | None], min_count: int = 3) -> str:
    nums = [float(v) for v in values if v is not None]
    if len(nums) < min_count:
        return f'样本不足（可算R {len(nums)}）'
    return str(round(sum(nums) / len(nums), 2))


def _median(values: list[float | None]) -> float | None:
    nums = sorted(float(v) for v in values if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return round(nums[mid], 2)
    return round((nums[mid - 1] + nums[mid]) / 2, 2)


def _metric_value(value: Any, default: float) -> float:
    parsed = _float_or_none(value)
    return parsed if parsed is not None else default


def _rate_label(value: float | None) -> str:
    return '样本不足' if value is None else f'{value * 100:.1f}%'


def _grade_is_win(grade: Any) -> bool:
    return grade in ('A', 'B', 'C', 'α', 'β')


def _grade_is_target(grade: Any) -> bool:
    return grade in ('A', 'B', 'α', 'β')


def _sample_ids(tasks: list[dict], limit: int = 5) -> list[str]:
    return [str(t.get('id') or '') for t in tasks[:limit] if t.get('id')]


def _setup_key(task: dict) -> str:
    snap = task.get('pred_snapshot') or {}
    return str(snap.get('setup_type') or task.get('strategy_tag') or '未分类')


def _opportunity_key(task: dict) -> str:
    snap = task.get('pred_snapshot') or {}
    return str(
        snap.get('opportunity_level')
        or snap.get('opportunity_grade')
        or task.get('final_rating')
        or '未分级'
    )


_SCOPE_LABELS = {
    'trade': '模拟交易/轻仓试错',
    'direction_watch': '方向观察/偏多未触发',
    'neutral_watch': '中性观察/弱观察',
    'avoid_watch': '回避观察/风险过滤',
}


def _scope_key(task: dict) -> str:
    dv = task.get('decision_view') if isinstance(task.get('decision_view'), dict) else {}
    scope = str((dv or {}).get('evaluation_scope') or task.get('evaluation_scope') or '').strip()
    if scope:
        return scope
    detail = _detail(task)
    task_class = str(detail.get('task_class') or task.get('task_class') or '')
    if task_class == 'buy':
        return 'trade'
    if task_class == 'avoid':
        return 'avoid_watch'
    if task.get('direction') == 'bullish' or task.get('final_action') == 'buy':
        return 'direction_watch'
    return 'neutral_watch'


def _scope_label(scope: Any) -> str:
    return _SCOPE_LABELS.get(str(scope or ''), str(scope or '未分层'))


def _cn_report_label(value: Any) -> str:
    text = str(value or '').strip()
    mapping = {
        '': '未分级',
        'None': '未分级',
        'none': '未分级',
        'hold': '观望/持有',
        'watch': '观察单',
        'neutral': '中性',
        'buy': '看多',
        'sell': '看空',
        'long': '看多',
        'short': '看空',
        'trend_breakout': '趋势突破',
        'box_breakout': '箱体突破',
        'pullback_buy': '回调买入',
        'oversold_rebound': '超跌反弹',
        'event_momentum': '事件驱动',
        'downtrend_avoid': '下跌回避',
    }
    return mapping.get(text, text)


def _grade_text(grade: Any) -> str:
    return str(grade or '').strip()


def _is_failure_case(record: dict) -> bool:
    grade = _grade_text(record.get('grade'))
    final_pct = _float_or_none(record.get('final_pct'))
    if record.get('sample_type') == 'watch':
        text = ' '.join(str(record.get(k) or '') for k in (
            'watch_quality', 'execution_quality', 'retrospective_summary',
        ))
        return (
            grade in ('D', 'E', 'F', 'δ')
            or grade.lower() == 'loss'
            or any(k in text for k in ('回避失效', '错过强机会', '错过大机会'))
        )
    if grade in ('D', 'E', 'F', 'δ') or grade.lower() == 'loss':
        return True
    return final_pct is not None and final_pct < 0


def _watch_success_case(record: dict) -> bool:
    if record.get('sample_type') != 'watch':
        return False
    text = ' '.join(str(record.get(k) or '') for k in (
        'watch_quality', 'execution_quality', 'retrospective_summary',
    ))
    return any(k in text for k in ('成功避险', '避免亏损', '有效回避'))


def _success_tier(record: dict) -> str | None:
    if _is_failure_case(record):
        return None
    grade = _grade_text(record.get('grade'))
    r = _float_or_none(record.get('r_multiple'))
    final_pct = _float_or_none(record.get('final_pct'))
    strong_grade = grade in ('A', 'B', 'α', 'β')
    if strong_grade:
        if r is None and final_pct is None:
            return None
        if (r is not None and r > 1.5) or (final_pct is not None and final_pct > 3):
            return 'strong'
        return 'qualified'
    if grade in ('C', 'γ') and final_pct is not None and final_pct >= 0:
        return 'qualified'
    if final_pct is not None and final_pct > 0:
        return 'qualified'
    if _watch_success_case(record):
        return 'qualified'
    return None


def _is_success_case(record: dict) -> bool:
    return _success_tier(record) is not None


def _case_sort_value(record: dict, success: bool) -> float:
    r_value = _float_or_none(record.get('r_multiple'))
    if r_value is not None:
        return r_value
    final_pct = _float_or_none(record.get('final_pct'))
    if final_pct is not None:
        return final_pct
    return -999999 if success else 999999


def _aggregator_insight(agent_perf: dict, overall_win_rate: float | None) -> dict:
    agent_hit_rates: list[float] = []
    per_agent: dict[str, float] = {}
    misleading_agents = 0
    total_agents = 0
    for label, data in (agent_perf or {}).items():
        hit = int(data.get('hit') or 0)
        mis = int(data.get('misleading') or 0)
        useful = hit + mis
        if useful >= 3:
            rate = hit / useful
            agent_hit_rates.append(rate)
            total_agents += 1
            if mis > hit:
                misleading_agents += 1
            per_agent[label] = round(rate, 3)
    avg_agent_hit = sum(agent_hit_rates) / len(agent_hit_rates) if agent_hit_rates else None
    correction = (
        round(overall_win_rate - avg_agent_hit, 3)
        if overall_win_rate is not None and avg_agent_hit is not None
        else None
    )
    per_agent_str = '、'.join(
        f'{lbl} {rate:.1%}' for lbl, rate in per_agent.items()
    ) if per_agent else '样本不足'

    if total_agents == 0:
        text = 'Agent 层样本不足，无法评估聚合矫正效应。'
    elif correction is not None and correction > 0.15:
        text = (
            f'Agent 层 {misleading_agents}/{total_agents} 误导偏多（{per_agent_str}），'
            f'但经 F 层辩论 + G 层决策聚合后系统有效率 {_rate_label(overall_win_rate)}'
            f'（矫正 +{correction:.1%}）——聚合层有效纠正单 Agent 噪声，'
            f'改进重点应放在聚合逻辑稳定性而非单 Agent prompt。'
        )
    elif correction is not None and correction > 0:
        text = (
            f'Agent 层平均命中率 {avg_agent_hit:.1%}（{per_agent_str}），'
            f'系统有效率 {_rate_label(overall_win_rate)}（矫正 +{correction:.1%}）'
            f'——聚合层正向贡献，幅度中等。'
        )
    elif correction is not None and correction >= -0.05:
        text = (
            f'Agent 层与系统有效率基本持平（Agent 均 {avg_agent_hit:.1%} vs 系统 '
            f'{_rate_label(overall_win_rate)}，{per_agent_str}）——聚合层未体现明显矫正，'
            f'需检查 F 层辩论是否流于形式、G 层权重是否过度依赖某一 Agent。'
        )
    elif correction is not None:
        text = (
            f'Agent 层平均命中率 {avg_agent_hit:.1%} 但系统有效率仅 '
            f'{_rate_label(overall_win_rate)}（矫正 {correction:.1%}，负值）——'
            f'聚合层可能丢失了单 Agent 信号（{per_agent_str}），'
            f'需排查 F 层审查是否过度否决、G 层决策链是否存在信号衰减。'
        )
    else:
        text = (
            f'Agent 层 {misleading_agents}/{total_agents} 误导偏多（{per_agent_str}），'
            f'系统有效率 {_rate_label(overall_win_rate)}。'
        )
    return {
        'avg_agent_hit': round(avg_agent_hit, 3) if avg_agent_hit is not None else None,
        'correction': correction,
        'per_agent': per_agent,
        'text': text,
    }


def _build_case_summary(records: list[dict]) -> dict[str, list[dict]]:
    strong_success: list[dict] = []
    qualified_success: list[dict] = []
    failure: list[dict] = []
    neutral: list[dict] = []
    missed_opportunity: list[dict] = []
    strong_ids: set[str] = set()
    failure_ids: set[str] = set()

    for record in records:
        rid = str(record.get('id') or '')
        final_pct = _float_or_none(record.get('final_pct'))
        r_value = _float_or_none(record.get('r_multiple'))
        scope = _scope_key(record)
        if r_value is None and final_pct is not None and final_pct >= 4 and scope in ('neutral_watch', 'direction_watch'):
            missed_opportunity.append(record)
        if _is_failure_case(record):
            failure.append(record)
            if rid:
                failure_ids.add(rid)
        else:
            tier = _success_tier(record)
            if tier == 'strong':
                strong_success.append(record)
                if rid:
                    strong_ids.add(rid)
            elif tier == 'qualified':
                qualified_success.append(record)
            else:
                neutral.append(record)

    if strong_ids:
        qualified_success = [r for r in qualified_success if str(r.get('id') or '') not in strong_ids]

    strong_success.sort(key=lambda r: _case_sort_value(r, True), reverse=True)
    qualified_success.sort(key=lambda r: _case_sort_value(r, True), reverse=True)
    failure.sort(key=lambda r: _case_sort_value(r, False))
    neutral.sort(key=lambda r: abs(_metric_value(r.get('final_pct'), 0)), reverse=True)
    missed_opportunity.sort(key=lambda r: _metric_value(r.get('final_pct'), 0), reverse=True)
    return {
        'strong_success': strong_success[:5],
        'qualified_success': qualified_success[:5],
        'failure': failure[:5],
        'neutral': neutral[:5],
        'missed_opportunity': missed_opportunity[:5],
    }


def _r_missing_reason(record: dict) -> str:
    if record.get('r_multiple') is not None:
        return ''
    action = str(record.get('final_action') or '').strip().lower()
    no_entry_actions = {'watch', 'observe', 'avoid', 'hold', 'wait', 'none', 'no_trade', '观望', '回避', '未入场'}
    if record.get('sample_type') == 'watch' or action in no_entry_actions:
        return '观察单/未入场：R 倍数不适用'
    has_stop = any(
        _float_or_none(record.get(k)) is not None
        for k in ('stop_pct', 'stop_price', 'invalid_price', 'invalidate_price')
    )
    if not has_stop:
        return '缺少止损/失效价：无法计算风险单位'
    if record.get('mae_r') is None and record.get('mfe_r') is None:
        return '缺少路径数据：无法计算 MAE/MFE'
    return 'R 倍数暂不可计算'


def _build_data_quality_notes(meta: dict, summary: dict, records: list[dict]) -> list[str]:
    notes: list[str] = []
    closed_count = int(meta.get('closed_count') or 0)
    if closed_count < 5:
        notes.append(f'已结算样本仅 {closed_count} 个，有效率、强兑现率和模式表现暂不适合作为稳定结论。')

    reason_counts: dict[str, int] = {}
    for record in records:
        reason = _r_missing_reason(record)
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    if reason_counts:
        reason_text = '；'.join(f'{reason} {count} 条' for reason, count in reason_counts.items())
        notes.append(f'R 倍数缺失原因：{reason_text}。')
    return notes


def _group_performance(tasks: list[dict], key_func) -> dict:
    groups: dict[str, list[dict]] = {}
    for task in tasks:
        groups.setdefault(key_func(task), []).append(task)

    result: dict[str, dict] = {}
    for key, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        closed = [t for t in rows if t.get('status') == 'closed' and t.get('grade')]
        trade_closed = []
        base = closed
        win_rate_basis = '观察样本'
        win_rate = None
        target_rate = None
        if len(base) >= 5:
            win_rate = sum(1 for t in base if _grade_is_win(t.get('grade'))) / len(base)
            target_rate = sum(1 for t in base if _grade_is_target(t.get('grade'))) / len(base)
        problem_counts: dict[str, int] = {}
        for t in rows:
            for flag in t.get('risk_flags') or []:
                problem_counts[str(flag)] = problem_counts.get(str(flag), 0) + 1
            rc = str(t.get('root_cause') or '').strip()
            if rc:
                problem_counts[rc] = problem_counts.get(rc, 0) + 1
        typical = [k for k, _v in sorted(problem_counts.items(), key=lambda x: -x[1])[:3]]
        # 盈亏比/期望值（与 tools/_backtest_dipbuy.py 同口径，按 r_multiple）
        _rv = [r for r in (_float_or_none(_detail(t).get('r_multiple')) for t in closed) if r is not None]
        _wins = [r for r in _rv if r > 0]
        _losses = [abs(r) for r in _rv if r < 0]
        _avg_loss = (sum(_losses) / len(_losses)) if _losses else 0.0
        payoff_ratio = round((sum(_wins) / len(_wins)) / _avg_loss, 2) if (_wins and _avg_loss > 0) else None
        expectancy_r = round(sum(_rv) / len(_rv), 3) if _rv else None
        result[key] = {
            'sample_count': len(rows),
            'closed_count': len(closed),
            'trade_count': len(trade_closed),
            'win_rate_basis': win_rate_basis,
            'win_rate': win_rate,
            'win_rate_label': _rate_label(win_rate),
            'target_rate': target_rate,
            'target_rate_label': _rate_label(target_rate),
            'avg_r': _avg([_float_or_none(_detail(t).get('r_multiple')) for t in closed]),
            'avg_r_label': _avg_label([_float_or_none(_detail(t).get('r_multiple')) for t in closed]),
            'avg_return_pct': _avg([_float_or_none(_detail(t).get('final_pct')) for t in closed]),
            'payoff_ratio': payoff_ratio,
            'expectancy_r': expectancy_r,
            'expectancy_pct': _avg([_float_or_none(_detail(t).get('final_pct')) for t in closed]),
            'typical_problems': typical or ['样本不足'],
            'sample_ids': _sample_ids(rows),
        }
    return result


def _problem_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in rows:
        for flag in task.get('risk_flags') or []:
            counts[str(flag)] = counts.get(str(flag), 0) + 1
        rc = str(task.get('root_cause') or '').strip()
        if rc:
            counts[rc] = counts.get(rc, 0) + 1
        pq = str(_detail(task).get('path_quality') or '').strip()
        if pq:
            counts[pq] = counts.get(pq, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _scope_drag(
    cid: str,
    title: str,
    count: int,
    rows: list[dict],
    suggestion: str,
    *,
    severity: str = 'medium',
) -> dict | None:
    if count <= 0:
        return None
    return {
        'id': cid,
        'title': title,
        'count': count,
        'sample_ids': _sample_ids(rows),
        'severity': severity,
        'suggestion': suggestion,
    }


def _scope_performance(tasks: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {key: [] for key in _SCOPE_LABELS}
    for task in tasks:
        groups.setdefault(_scope_key(task), []).append(task)

    result: dict[str, dict] = {}
    for scope, rows in groups.items():
        closed = [t for t in rows if t.get('status') == 'closed' and t.get('grade')]
        final_values = [_float_or_none(_detail(t).get('final_pct')) for t in closed]
        r_values = [_float_or_none(_detail(t).get('r_multiple')) for t in closed]
        grade_counts: dict[str, int] = {}
        for t in closed:
            g = str(t.get('grade') or '')
            grade_counts[g] = grade_counts.get(g, 0) + 1

        base_count = len(closed)
        win_rate = (
            sum(1 for t in closed if _grade_is_win(t.get('grade'))) / base_count
            if base_count >= 3 else None
        )
        target_rate = (
            sum(1 for t in closed if _grade_is_target(t.get('grade'))) / base_count
            if base_count >= 3 else None
        )
        problem_counts = _problem_counts(closed)
        stressed = [t for t in closed if _detail(t).get('path_quality') == 'P_stressed']
        losses = [t for t in closed if _float_or_none(_detail(t).get('r_multiple')) is not None and (_float_or_none(_detail(t).get('r_multiple')) or 0) < 0]
        neutral_big_miss = [
            t for t in closed
            if _float_or_none(_detail(t).get('final_pct')) is not None
            and (_float_or_none(_detail(t).get('final_pct')) or 0) > 6
        ]
        direction_upside = [
            t for t in closed
            if _float_or_none(_detail(t).get('final_pct')) is not None
            and (_float_or_none(_detail(t).get('final_pct')) or 0) >= 4
        ]
        direction_failed = [
            t for t in closed
            if t.get('grade') in ('D', 'E', 'F', 'δ')
            or ((_float_or_none(_detail(t).get('final_pct')) or 0) <= -4)
        ]
        avoid_failed = [
            t for t in closed
            if t.get('grade') in ('D', 'E', 'F', 'δ')
            or ((_float_or_none(_detail(t).get('final_pct')) or 0) > 0)
        ]

        drag_factors: list[dict] = []
        if scope == 'trade':
            for item in (
                _scope_drag(
                    'scope_trade_path_stress',
                    '交易样本路径压力过大',
                    len(stressed),
                    stressed,
                    '把 P_stressed、MAE_R 和技术过热合并为入场前风险闸，减少尾部亏损。',
                ),
                _scope_drag(
                    'scope_trade_loss_tail',
                    '交易样本亏损尾部',
                    len(losses),
                    losses,
                    '复核亏损样本的失效价、止损宽度和入场触发，先压缩亏损尾部再谈升权。',
                ),
            ):
                if item:
                    drag_factors.append(item)
        elif scope == 'neutral_watch':
            item = _scope_drag(
                'scope_neutral_watch_big_miss',
                '中性观察错过强上涨',
                len(neutral_big_miss),
                neutral_big_miss,
                '复核 C 弱观察的主线扩散、板块共振和资金确认阈值，避免把强势扩散行情压成中性。',
            )
            if item:
                drag_factors.append(item)
        elif scope == 'direction_watch':
            for item in (
                _scope_drag(
                    'scope_direction_watch_unconverted_upside',
                    '偏多观察兑现但未转入场',
                    len(direction_upside),
                    direction_upside,
                    '检查 bullish_watch 的触发价、放量约束和转换逻辑，区分正确等待与错过确认。',
                    severity='low',
                ),
                _scope_drag(
                    'scope_direction_watch_failed',
                    '偏多观察方向失败',
                    len(direction_failed),
                    direction_failed,
                    '复核 B/A 观察的防守位、技术数据不足和资金背离，避免候选形态过早偏多。',
                ),
            ):
                if item:
                    drag_factors.append(item)
        elif scope == 'avoid_watch':
            item = _scope_drag(
                'scope_avoid_watch_failed',
                '回避观察失效',
                len(avoid_failed),
                avoid_failed,
                '回避样本若上涨，复核 fail_level 和修复条件；若样本少，仅作为风险过滤校准。',
            )
            if item:
                drag_factors.append(item)

        result[scope] = {
            'label': _scope_label(scope),
            'sample_count': len(rows),
            'closed_count': len(closed),
            'win_rate': win_rate,
            'win_rate_label': _rate_label(win_rate),
            'target_rate': target_rate,
            'target_rate_label': _rate_label(target_rate),
            'avg_return_pct': _avg(final_values),
            'r_count': len([r for r in r_values if r is not None]),
            'avg_r': _avg(r_values),
            'avg_r_label': _avg_label(r_values),
            'median_r': _median(r_values),
            'median_r_label': str(_median(r_values)) if _median(r_values) is not None else '样本不足',
            'grade_counts': grade_counts,
            'problem_counts': problem_counts,
            'typical_problems': list(problem_counts.keys())[:3] or ['样本不足'],
            'drag_factors': drag_factors,
            'big_miss_count': len(neutral_big_miss) if scope == 'neutral_watch' else 0,
            'upside_validated_count': len(direction_upside) if scope == 'direction_watch' else 0,
            'direction_failed_count': len(direction_failed) if scope == 'direction_watch' else 0,
            'avoid_failed_count': len(avoid_failed) if scope == 'avoid_watch' else 0,
            'sample_ids': _sample_ids(rows),
        }
    return result


def _agent_performance(tasks: list[dict]) -> dict:
    rows: dict[str, dict] = {}
    for task in tasks:
        detail = _detail(task)
        hit_map = detail.get('agent_hit_map') or _agent_hit_map(task, _float_or_none(detail.get('final_pct')) or 0.0)
        for _key, item in hit_map.items():
            label = item.get('label') or _key
            row = rows.setdefault(label, {
                'sample_count': 0,
                'hit': 0,
                'misleading': 0,
                'insufficient': 0,
                'sample_ids': [],
                'hit_sample_ids': [],
                'misleading_sample_ids': [],
            })
            row['sample_count'] += 1
            status = item.get('status')
            if status == '命中':
                row['hit'] += 1
                if task.get('id') and len(row['hit_sample_ids']) < 5:
                    row['hit_sample_ids'].append(task.get('id'))
            elif status == '误导':
                row['misleading'] += 1
                if task.get('id') and len(row['misleading_sample_ids']) < 5:
                    row['misleading_sample_ids'].append(task.get('id'))
            else:
                row['insufficient'] += 1
            if task.get('id') and len(row['sample_ids']) < 5:
                row['sample_ids'].append(task.get('id'))

    for row in rows.values():
        useful = row['hit'] + row['misleading']
        row['hit_rate'] = round(row['hit'] / useful, 3) if useful >= 3 else None
        row['status'] = '样本不足' if useful < 3 else ('有效信号较多' if row['hit'] >= row['misleading'] else '误导偏多')
    return rows


def _add_diag(diags: dict[str, dict], label: str, task: dict) -> None:
    row = diags.setdefault(label, {'count': 0, 'sample_ids': []})
    row['count'] += 1
    if task.get('id') and len(row['sample_ids']) < 5:
        row['sample_ids'].append(task.get('id'))


def _risk_diagnostics(tasks: list[dict]) -> dict:
    diags: dict[str, dict] = {}
    for task in tasks:
        flags = [str(f) for f in (task.get('risk_flags') or [])]
        for flag in flags:
            if '资金' in flag:
                _add_diag(diags, '资金背离', task)
            elif '事件' in flag or '情报' in flag:
                _add_diag(diags, '事件未验证', task)
            elif '技术过热' in flag or '超买' in flag:
                _add_diag(diags, '技术过热', task)
            elif '观望' in flag:
                _add_diag(diags, '观察/回避信号', task)
            else:
                _add_diag(diags, flag, task)
        detail = _detail(task)
        if task.get('sample_type') == 'watch' and task.get('grade') in ('A', 'B', 'α', 'β'):
            _add_diag(diags, '回避失效', task)
        mae_r = _float_or_none(detail.get('mae_r'))
        if mae_r is not None and mae_r <= -1:
            _add_diag(diags, '止损不合理', task)
        if detail.get('path_quality') == 'P_stressed':
            _add_diag(diags, '路径压力过大', task)
    return dict(sorted(diags.items(), key=lambda item: -item[1]['count']))


def _error_analysis(tasks: list[dict]) -> dict:
    by_error: dict[str, dict] = {}
    by_root: dict[str, dict] = {}
    for task in tasks:
        et = str(task.get('error_type') or 'N')
        row = by_error.setdefault(et, {'count': 0, 'sample_ids': []})
        row['count'] += 1
        if task.get('id') and len(row['sample_ids']) < 5:
            row['sample_ids'].append(task.get('id'))

        rc = str(task.get('root_cause') or '').strip()
        if rc:
            root = by_root.setdefault(rc, {'count': 0, 'sample_ids': []})
            root['count'] += 1
            if task.get('id') and len(root['sample_ids']) < 5:
                root['sample_ids'].append(task.get('id'))
    return {
        'by_error_type': dict(sorted(by_error.items(), key=lambda item: -item[1]['count'])),
        'by_root_cause': dict(sorted(by_root.items(), key=lambda item: -item[1]['count'])),
    }


def _record_for_report(task: dict, include_agent_summary: bool = False) -> dict:
    detail = _detail(task)
    dv = task.get('decision_view') or {}
    row = {
        'id': task.get('id'),
        'created_at': task.get('created_at'),
        'closed_at': task.get('closed_at'),
        'code': task.get('code'),
        'name': task.get('name'),
        'kind': task.get('kind', 'stock'),
        'sample_type': task.get('sample_type'),
        'setup': _setup_key(task),
        'opportunity': _opportunity_key(task),
        'direction': task.get('direction'),
        'bias_label': dv.get('bias_label') or '',
        'action_label': dv.get('action_label') or '',
        'evaluation_scope': dv.get('evaluation_scope') or '',
        'entry_price': task.get('entry_price'),
        'stop_pct': task.get('stop_pct'),
        'stop_price': task.get('stop_price'),
        'invalid_price': task.get('invalid_price') or task.get('invalidate_price'),
        'final_action': task.get('final_action'),
        'grade': task.get('grade'),
        'final_pct': detail.get('final_pct'),
        'r_multiple': detail.get('r_multiple'),
        'mae_r': detail.get('mae_r'),
        'mfe_r': detail.get('mfe_r'),
        'capture_ratio': detail.get('capture_ratio'),
        'execution_quality': detail.get('execution_quality'),
        'watch_quality': detail.get('watch_quality'),
        'error_type': task.get('error_type'),
        'root_cause': task.get('root_cause'),
        'risk_flags': task.get('risk_flags') or [],
        'retrospective_summary': str(task.get('retrospective') or '')[:200],
    }
    row['r_missing_reason'] = _r_missing_reason(row)
    if include_agent_summary:
        snap = task.get('pred_snapshot') or {}
        row['agent_summary'] = {
            k: str(snap.get(k) or '')[:300]
            for k in ('_agent_macro', '_agent_company', '_agent_technical', '_agent_fundflow', '_agent_news')
            if snap.get(k)
        }
    return row


def _audit_log_summary(events: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    warnings: list[dict] = []
    for event in events:
        et = str(event.get('event_type') or 'unknown')
        by_type[et] = by_type.get(et, 0) + 1
        if event.get('severity') in ('warning', 'error') and len(warnings) < 10:
            warnings.append({
                'timestamp': event.get('timestamp'),
                'event_type': et,
                'task_id': event.get('task_id'),
                'message': event.get('message'),
            })
    return {'total_events': len(events), 'by_type': by_type, 'warnings': warnings}


def _load_alert_logs(days: int | None = None) -> list[dict]:
    path = CACHE_DIR / 'alert_log.json'
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    if not days:
        return [e for e in data if isinstance(e, dict)]
    cutoff = date.today() - timedelta(days=days)
    out = []
    for event in data:
        if not isinstance(event, dict):
            continue
        event_date = _parse_date_prefix(event.get('timestamp'))
        if event_date and event_date >= cutoff:
            out.append(event)
    return out


def _top_problem_text(report: dict, limit: int = 5) -> str:
    rows: list[str] = []
    for label, data in list((report.get('risk_diagnostics') or {}).items())[:limit]:
        ids = ', '.join((data.get('sample_ids') or [])[:3])
        rows.append(f'{label} {data.get("count", 0)} 次（{ids or "无代表样本"}）')
    if rows:
        return '；'.join(rows)
    return '样本不足，暂未形成稳定问题。'


def _candidate_prompt_text(candidates: list[dict], limit: int = 5) -> str:
    rows = []
    for item in candidates[:limit]:
        ids = ', '.join((item.get('sample_ids') or [])[:3])
        rows.append(f'{item.get("title")}：{item.get("evidence_count", 0)} 个样本（{ids or "无代表样本"}）')
    return '；'.join(rows) if rows else '暂无。'


def _layered_diag_text(report: dict, limit: int = 6) -> str:
    rows = []
    for item in (report.get('layered_diagnostics') or [])[:limit]:
        ids = ', '.join((item.get('sample_ids') or [])[:3])
        rows.append(f'{item.get("layer")}：{item.get("title")} {item.get("count", 0)} 个样本（{ids or "无代表样本"}）')
    return '；'.join(rows) if rows else '暂无。'


def _case_id_text(records: list[dict]) -> str:
    ids = [str(r.get('id') or '') for r in records if r.get('id')]
    return ', '.join(ids[:5]) if ids else '暂无。'


def _build_ai_prompt(report: dict) -> str:
    meta = report.get('meta', {})
    summary = report.get('summary', {})
    cases = report.get('case_summary') or {}
    notes = report.get('data_quality_notes') or []
    avg_r = summary.get('avg_r_label') or '样本不足'
    return '\n'.join([
        '请作为复盘分析助手阅读这份追踪报告，只分析系统性偏差，不给具体买卖推荐。',
        f'总览统计：样本 {meta.get("sample_count", 0)}；已结算 {meta.get("closed_count", 0)}；'
        f'观察样本 {summary.get("watch_count", 0)}；'
        f'方向有效率 {summary.get("win_rate_label", "样本不足")}；平均 R {avg_r}。',
        f'数据边界：{"；".join(notes[:3]) if notes else "暂无明显数据质量限制。"}',
        f'分层拖后腿：{_layered_diag_text(report)}',
        f'Top问题：{_top_problem_text(report)}',
        f'强成功案例ID：{_case_id_text(cases.get("strong_success") or [])}',
        f'达标案例ID：{_case_id_text(cases.get("qualified_success") or [])}',
        f'失败案例ID：{_case_id_text(cases.get("failure") or [])}',
        f'Agent 层矫正效应：{(report.get("aggregator_insight") or {}).get("text") or "样本不足"}',
        f'改进候选：{_candidate_prompt_text(report.get("improvement_candidates") or [])}',
        '输出要求：1. 系统性偏差；2. 哪些模式应升权/降权；'
        '3. 哪些 Agent 需要改 prompt 或数据边界；4. 下次循环优化前三个修改目标。',
        '禁止事项：不得新增事实；必须引用上面的统计项或样本 ID；样本不足必须明确说明；不允许给具体买卖推荐。',
    ])


def _candidate(
    cid: str,
    ctype: str,
    severity: str,
    title: str,
    evidence_count: int,
    sample_ids: list[str],
    suggestion: str,
    location: str,
) -> dict:
    return {
        'id': cid,
        'type': ctype,
        'severity': severity,
        'title': title,
        'evidence_count': evidence_count,
        'sample_ids': sample_ids,
        'evidence': f'命中 {evidence_count} 个样本，代表样本：{", ".join(sample_ids[:5])}',
        'suggestion': suggestion,
        'suggested_location': location,
        'status': 'proposed',
        'user_note': '',
    }


def _layered_diagnostics(report: dict) -> list[dict]:
    diagnostics: list[dict] = []
    meta = report.get('meta') or {}
    scopes = report.get('performance_by_scope') or {}
    closed_count = int(meta.get('closed_count') or 0)
    r_count = sum(int(row.get('r_count') or 0) for row in scopes.values())
    if closed_count and r_count < closed_count:
        diagnostics.append({
            'id': 'layer_data_r_coverage',
            'layer': '数据边界',
            'title': 'R 倍数只覆盖模拟交易样本',
            'count': closed_count - r_count,
            'sample_ids': [],
            'severity': 'high',
            'suggestion': '总览同时展示观察样本、模拟交易样本和 R 覆盖率，禁止把整体观察样本解释成实盘战绩。',
        })
    for scope, row in scopes.items():
        for item in row.get('drag_factors') or []:
            diagnostics.append({
                'id': item.get('id'),
                'layer': row.get('label') or _scope_label(scope),
                'title': item.get('title'),
                'count': item.get('count', 0),
                'sample_ids': item.get('sample_ids') or [],
                'severity': item.get('severity', 'medium'),
                'suggestion': item.get('suggestion') or '',
            })
    for label, data in (report.get('performance_by_agent') or {}).items():
        misleading = int(data.get('misleading') or 0)
        hit = int(data.get('hit') or 0)
        if misleading > hit and misleading > 0:
            diagnostics.append({
                'id': f'layer_agent_{label}',
                'layer': 'Agent 数据边界',
                'title': f'{label}误导偏多',
                'count': misleading,
                'sample_ids': data.get('misleading_sample_ids') or data.get('sample_ids') or [],
                'severity': 'medium',
                'suggestion': '复核该 Agent 的输入来源、样本天数和置信度表达，避免把缺数据或短窗口写成强判断。',
            })
    return diagnostics


def _improvement_candidates(report: dict) -> list[dict]:
    candidates: list[dict] = []
    for item in report.get('layered_diagnostics') or []:
        cid = str(item.get('id') or '')
        if not cid or cid == 'layer_data_r_coverage':
            continue
        count = int(item.get('count') or 0)
        ids = list(item.get('sample_ids') or [])
        if count <= 0:
            continue
        candidates.append(_candidate(
            cid,
            'scope_diagnostic' if cid.startswith('scope_') else 'data_quality',
            str(item.get('severity') or 'medium'),
            str(item.get('title') or '分层复盘口径问题'),
            count,
            ids,
            str(item.get('suggestion') or '按分层复盘口径复核样本。'),
            'core/tracking.py',
        ))
    agent_perf = report.get('performance_by_agent') or {}
    for label, data in agent_perf.items():
        misleading = int(data.get('misleading') or 0)
        if misleading > 0:
            ids = list(data.get('misleading_sample_ids') or []) or list(data.get('sample_ids') or [])[:misleading]
            candidates.append(_candidate(
                f'prompt_rule_agent_{label}', 'prompt_rule', 'medium',
                f'{label}存在误导样本', misleading, ids,
                '复核该 Agent 的数据边界和置信度表达，样本不足时不要给强结论。',
                'core/agents',
            ))
    diags = report.get('risk_diagnostics') or {}
    for label, data in diags.items():
        count = int(data.get('count') or 0)
        ids = list(data.get('sample_ids') or [])
        if count <= 0 or not ids:
            continue
        if label == '事件未验证':
            candidates.append(_candidate(
                'data_quality_event_unverified', 'data_quality', 'medium',
                '事件信号需要证据边界', count, ids,
                '事件类结论在缺少验证数据时降低置信表述，并要求写清数据来源。',
                'core/agents 或情报校验 prompt',
            ))
        elif label == '资金背离':
            candidates.append(_candidate(
                'prompt_rule_fundflow_divergence', 'prompt_rule', 'medium',
                '资金流信号存在背离风险', count, ids,
                '资金流 Agent 在短窗口或缺数据时禁止连续性表述，并提示样本不足。',
                'core/agents 或资金流摘要 prompt',
            ))
        elif label in ('技术过热', '路径压力过大', '止损不合理'):
            candidates.append(_candidate(
                f'scoring_logic_{label}', 'scoring_logic', 'medium',
                f'{label}需要复核', count, ids,
                '复核入场、止损和路径压力的提示文案，避免把高波动样本当作干净达标。',
                'core/tracking.py',
            ))
        elif label == '回避失效':
            candidates.append(_candidate(
                'deterministic_rule_watch_miss', 'deterministic_rule', 'low',
                '观察单可能错过强机会', count, ids,
                'Review observation-sample boundaries only; do not auto-change sample classification.',
                'ui/tracking_panel.py',
            ))
    roots = (report.get('error_analysis') or {}).get('by_root_cause') or {}
    for root, data in roots.items():
        count = int(data.get('count') or 0)
        if count > 1:
            root_id = ''.join(ch for ch in root if ch.isalnum())[:24] or 'unknown'
            candidates.append(_candidate(
                f'prompt_rule_root_{root_id}', 'prompt_rule', 'medium',
                f'重复归因：{root[:30]}', count, list(data.get('sample_ids') or []),
                '把重复归因沉淀为下一轮提示词或确定性规则候选，先人工确认再应用。',
                'core/agents 或 core/tracking.py',
            ))
    unique: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        cid = str(item.get('id') or '')
        if cid in seen:
            continue
        seen.add(cid)
        unique.append(item)
    return unique


def save_improvement_candidates(candidates: list[dict]) -> None:
    """写入改进候选，保留同 ID 的人工状态和备注。"""
    existing: dict[str, dict] = {}
    if _IMPROVEMENT_FILE.exists():
        try:
            raw = json.loads(_IMPROVEMENT_FILE.read_text(encoding='utf-8'))
            if isinstance(raw, list):
                existing = {str(x.get('id')): x for x in raw if isinstance(x, dict)}
        except Exception:
            existing = {}
    merged: list[dict] = []
    for item in candidates:
        old = existing.get(str(item.get('id')))
        if old:
            item = dict(item)
            item['status'] = old.get('status') or item.get('status', 'proposed')
            item['user_note'] = old.get('user_note') or item.get('user_note', '')
        merged.append(item)
    try:
        _IMPROVEMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _IMPROVEMENT_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        logger.warning('[tracking] 保存改进候选失败: %s', e)


def build_tracking_report_data(options: dict | None = None) -> dict:
    """构建复盘报告数据；只读本地任务、审计日志和已有反思结果，不调用 AI。"""
    options = dict(options or {})
    tasks = _filter_report_tasks(load_tasks(), options)
    events = load_tracking_events()
    days = _range_days(options.get('range_days'))
    if days:
        cutoff = date.today() - timedelta(days=days)
        events = [
            e for e in events
            if (_parse_date_prefix(e.get('timestamp')) or date.min) >= cutoff
        ]
    alert_logs = _load_alert_logs(days)
    try:
        from core.analysis_memory import get_stats as _memory_stats
        memory_stats = _memory_stats()
    except Exception:
        memory_stats = {'codes': 0, 'blocks': 0}

    closed = [t for t in tasks if t.get('status') == 'closed' and t.get('grade')]
    trade_closed = []
    watch_closed = [t for t in closed if t.get('sample_type') == 'watch']
    formal_base = [t for t in watch_closed if t.get('kind') != 'sector']
    win_rate = None
    target_rate = None
    if len(formal_base) >= 5:
        win_rate = sum(1 for t in formal_base if _grade_is_win(t.get('grade'))) / len(formal_base)
        target_rate = sum(1 for t in formal_base if _grade_is_target(t.get('grade'))) / len(formal_base)

    final_values = [_float_or_none(_detail(t).get('final_pct')) for t in closed]
    r_values = [_float_or_none(_detail(t).get('r_multiple')) for t in closed]
    max_loss_task = min(closed, key=lambda t: _metric_value(_detail(t).get('final_pct'), 999999), default=None)
    max_drawdown_task = min(closed, key=lambda t: _metric_value(_detail(t).get('mae_r'), 999999), default=None)
    case_records = [_record_for_report(t) for t in closed]

    # Direction-observation accuracy for watch samples.
    watch_with_direction = [
        t for t in watch_closed
        if t.get('direction') in ('bullish', 'bearish')
        and _detail(t).get('final_pct') is not None
    ]
    direction_correct = sum(
        1 for t in watch_with_direction
        if (t.get('direction') == 'bullish' and (_detail(t).get('final_pct') or 0) > 0)
        or (t.get('direction') == 'bearish' and (_detail(t).get('final_pct') or 0) < 0)
    )
    direction_accuracy = (
        round(direction_correct / len(watch_with_direction), 3)
        if len(watch_with_direction) >= 3 else None
    )
    direction_watch_stats = {
        'total': len(watch_with_direction),
        'correct': direction_correct,
        'accuracy': direction_accuracy,
        'accuracy_label': _rate_label(direction_accuracy) if direction_accuracy is not None else '样本不足',
    }

    report = {
        'meta': {
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'range_days': days or 'all',
            'sample_scope': options.get('sample_scope') or 'all',
            'kind_scope': options.get('kind_scope') or 'all',
            'sample_count': len(tasks),
            'closed_count': len(closed),
            'analysis_memory_stats': memory_stats,
            'alert_log_count': len(alert_logs),
        },
        'summary': {
            'trade_count': len([t for t in tasks if t.get('sample_type') == 'trade']),
            'watch_count': len([t for t in tasks if t.get('sample_type') == 'watch']),
            'closed_trade_count': len(trade_closed),
            'closed_watch_count': len(watch_closed),
            'win_rate': win_rate,
            'win_rate_label': _rate_label(win_rate),
            'target_rate': target_rate,
            'target_rate_label': _rate_label(target_rate),
            'avg_return_pct': _avg(final_values),
            'avg_r': _avg(r_values),
            'avg_r_label': _avg_label(r_values),
            'max_loss': _record_for_report(max_loss_task) if max_loss_task else None,
            'max_drawdown_sample': _record_for_report(max_drawdown_task) if max_drawdown_task else None,
            'direction_watch_stats': direction_watch_stats,
        },
        'performance_by_scope': _scope_performance(closed),
        'performance_by_setup': _group_performance(closed, _setup_key),
        'performance_by_opportunity': _group_performance(closed, _opportunity_key),
        'performance_by_agent': _agent_performance(closed),
        'risk_diagnostics': _risk_diagnostics(closed),
        'error_analysis': _error_analysis(closed),
        'audit_log_summary': _audit_log_summary(events),
        'data_quality_notes': [],
        'case_summary': {},
        'records': [],
    }
    report['data_quality_notes'] = _build_data_quality_notes(report['meta'], report['summary'], case_records)
    report['case_summary'] = _build_case_summary(case_records)
    report['layered_diagnostics'] = _layered_diagnostics(report)
    report['improvement_candidates'] = _improvement_candidates(report)
    report['aggregator_insight'] = _aggregator_insight(report['performance_by_agent'], win_rate)
    report['ai_prompt'] = _build_ai_prompt(report)
    if options.get('include_records', True):
        include_agent = bool(options.get('include_agent_summary'))
        report['records'] = [_record_for_report(t, include_agent) for t in tasks]
    return report


# ─────────────────────────────────────────────
# AI 反思 Prompt
# ─────────────────────────────────────────────

def _plan_brief(task: dict) -> str:
    tc = task.get('task_class')
    target = float(task.get('target_pct') or 0)
    stop = float(task.get('stop_pct') or 0)
    if tc == 'buy':
        if target > 0 and stop != 0:
            return f'目标涨幅: {target:.1f}%  |  止损线: -{abs(stop):.1f}%'
        return '看多观察（未设目标/止损，按到期收盘评看多是否兑现）'
    if tc == 'avoid':
        return '看跌回避信号（A股散户不做空，评回避是否准确：实际跌=避对 / 涨=踏空）'
    return '中性观望（无交易目标，评是否躲过下跌 / 错过上涨 / 横盘合理）'


def build_retrospective_prompt(
    task: dict,
    money_flow_history: list[dict],
    emotion_history: list[dict],
    history_memory: str = '',
) -> str:
    """组装 AI 反思 prompt，供 _RetrospectiveWorker 调用。

    token 控制策略：
      - 资金流 / 情绪序列：周采样（每 5 个取 1 个）
      - intel_snapshot：最多 10 条，按 level 降序
      - 5 Agent 各取前 200 字
    """
    direction = task.get('direction', '多')
    entry_price = task.get('entry_price', 0)
    deadline = task.get('deadline', '')
    grade = task.get('grade', 'F')
    detail = task.get('grade_detail') or {}
    pred_snap = task.get('pred_snapshot') or {}
    intel_snap = task.get('intel_snapshot') or []

    final_pct = detail.get('final_pct', 0)
    outcome_tier = detail.get('outcome_tier', '')
    path_quality = detail.get('path_quality', '')
    trough_pct = detail.get('trough_pct', 0)
    peak_pct = detail.get('peak_pct', 0)
    directional_metric_lines = format_directional_metric_lines(detail, task)

    # 5 Agent 摘要（各 200 字）
    def _trim(text: str, n: int = 200) -> str:
        text = str(text or '').strip()
        return text[:n] + '…' if len(text) > n else text

    macro_sum = _trim(pred_snap.get('_agent_macro', ''))
    company_sum = _trim(pred_snap.get('_agent_company', ''))
    tech_sum = _trim(pred_snap.get('_agent_technical', ''))
    fundflow_sum = _trim(pred_snap.get('_agent_fundflow', ''))
    news_sum = _trim(pred_snap.get('_agent_news', ''))

    # 情报快照（最多 10 条，按 level 降序）
    _LEVEL_ORDER = {'critical': 0, 'important': 1, 'info': 2}
    top_intel = sorted(
        (e for e in intel_snap if isinstance(e, dict)),
        key=lambda e: _LEVEL_ORDER.get(str(e.get('level', 'info')), 9)
    )[:10]
    intel_lines = '\n'.join(
        f"  [{e.get('level','?')}] {e.get('date','?')} {e.get('title','')}"
        for e in top_intel
    ) or '  （无快照情报）'

    # 资金流周采样（每 5 天取 1 个，保留 date + net_flow）
    flow_sampled = money_flow_history[::5][-6:]
    flow_lines = '\n'.join(
        f"  {r.get('date','?')} 净流: {r.get('net_flow', 0)/1e8:.2f}亿"
        for r in flow_sampled
    ) or '  （无缓存）'

    # 大盘情绪周采样（fear_greed 或 score 字段）
    emo_sampled = emotion_history[::5][-8:]
    emo_lines = '\n'.join(
        f"  {r.get('date','?')} 情绪={r.get('fear_greed') or r.get('score', '?')}"
        for r in emo_sampled
    ) or '  （无缓存）'

    # ── 客观裁判信号（系统已确定性计算）──────────────────────────
    hit_map = detail.get('agent_hit_map') or {}
    _hit_label = {'命中': '✓命中', '误导': '✗误导', '样本不足': '—样本不足'}
    hit_lines = '\n'.join(
        f"  {v.get('label','?')}: {_hit_label.get(v.get('status'), v.get('status','?'))}"
        f"（立场 {v.get('stance') or '未知'}）"
        for v in hit_map.values()
    ) or '  （无 Agent 命中数据）'

    metric_lines = format_retrospective_metric_lines(detail, task)

    # ── 数据质量门控 ──────────────────────────────────────────────
    dq = assess_reflection_data_quality(task)
    if dq['warnings']:
        dq_lines = '\n'.join(f'  ⚠ {w}' for w in dq['warnings'])
        dq_block = (
            f"数据质量档位: {dq['level']}（degraded=部分维度不可靠）\n{dq_lines}\n"
            f"  规则：被标 missing/weak 的维度，禁止编造因果归因，只能写“数据不足，不做归因”。"
        )
    else:
        dq_block = "数据质量档位: full（各维度数据充分）"

    history_block = history_memory.strip() or '（无该股历史反思记录）'

    prompt = f"""你是一位专业 A 股量化分析师，正在对一次预测追踪任务做事后反思分析。
你必须客观、反事后偏差（不因结果好坏倒推决策对错），且只能引用下文出现的数字与信号。

## 任务基本信息
- 股票: {task.get('name','')}（{task.get('code','')}）
- 方向: {direction}  |  入场价: {entry_price}  |  审判日: {deadline}
- {_plan_brief(task)}
- 置信度: {task.get('confidence',0):.0f}/10

## 评级结果
- 最终评级: **{grade}**（{outcome_tier}，{path_quality}）
- {directional_metric_lines[0]}  |  {directional_metric_lines[1]}

## 客观裁判信号（系统已确定性计算，不得与之矛盾）
Agent 命中判定（以实际涨跌为准，系统判定）：
{hit_lines}
关键风险/收益指标：
{metric_lines}

## 数据质量（垃圾判断结果，务必遵守）
{dq_block}

## 预测时 5 Agent 观点摘要（原始文本，需结合上方命中判定解读）
宏观/情报 Agent: {macro_sum}

基本面 Agent:  {company_sum}
技术面 Agent: {tech_sum}
资金流向 Agent: {fundflow_sum}
新闻/事件 Agent: {news_sum}

## 追踪期间情报快照（预测创建时存档）
{intel_lines}

## 追踪期间资金流（周采样）
{flow_lines}

## 大盘情绪（周采样）
{emo_lines}

## 该股历史反思教训（用于识别是否重复犯错）
{history_block}

## 反思任务
请用中文撰写 300-500 字的反思分析。先在心里区分两件事，再落笔：
- **决策质量**：以预测当时可得的信息，做出该方向/目标/止损是否合理？（独立于结果）
- **结果质量**：实际涨跌与路径如何？（以上方客观信号为准陈述，不要自行编造数字）

正文需涵盖：
1. 过程×结果四象限判定（必写其一）：好过程好结果 / 好过程坏结果（运气差）/ 坏过程好结果（运气好）/ 坏过程坏结果，并一句话说明理由。
2. 哪个 Agent 命中/误导——必须与上方“Agent 命中判定”一致，不得反着说。
3. 情报、情绪面、资金流对实际走势的解释（仅在对应数据充分时展开）。
4. 路径质量（回撤深度、MAE/MFE）的启示：止损线设置是否合理。
5. 是否重复了“历史反思教训”里的同类错误？下次可改进的 1-2 条具体建议。

**硬约束**：
- 只能引用本 prompt 中出现的数字与信号，禁止臆造行情/资金/情绪数据。
- “数据质量”中被标 missing/weak 的维度，对应分析必须写“数据不足，不做归因”，不得编造因果。
- 错误类型 B（数据问题）仅当数据质量确有维度被标 missing 时才可使用。

**重要**：反思文本前两行必须严格遵循以下格式（两行均必须出现，无论结果好坏）：
[错误类型: X]
核心归因: <≤40字，跨任务可比，不复述事实，只标本任务关键归因点>
其中 X 为：A=模型可改进 / B=数据问题 / C=黑天鹅/外部冲击 / D=方向对但交易执行错 / N=无明显错误
注意：X 只能是 A/B/C/D/N 其中一个字母，禁止使用 αβγδ 等其他字符。

请直接输出反思文本，前两行为上述标注，后续为详细分析。"""

    return prompt


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _next_task_id(date_str: str, existing: list[dict]) -> str:
    """生成日内唯一 ID，格式 trk_{YYYYMMDD}_{seq:03d}。"""
    prefix = f'trk_{date_str}_'
    n = sum(1 for t in existing if isinstance(t.get('id'), str) and t['id'].startswith(prefix))
    return f'{prefix}{n + 1:03d}'


def _serialize_intel_snapshot(intel_events: list, code: str, name: str) -> list[dict]:
    """过滤并序列化情报快照（最多 50 条相关情报）。"""
    try:
        from core.intel_matcher import filter_relevant_intel
        relevant = filter_relevant_intel(intel_events, code, name, max_n=50)
    except Exception:
        relevant = intel_events[:50] if intel_events else []

    result = []
    for e in relevant:
        try:
            if hasattr(e, '__dict__'):
                d = {
                    'title': getattr(e, 'title', ''),
                    'level': getattr(e, 'level', 'info'),
                    'date': str(getattr(e, 'date', '')),
                    'summary': str(getattr(e, 'summary', ''))[:200],
                }
            elif isinstance(e, dict):
                d = {
                    'title': e.get('title', ''),
                    'level': e.get('level', 'info'),
                    'date': str(e.get('date', '')),
                    'summary': str(e.get('summary', ''))[:200],
                }
            else:
                continue
            result.append(d)
        except Exception:
            continue
    return result


def _kline_to_compact(df, start_date: date, end_date: date) -> list[dict]:
    """将 DataFrame 切片转为紧凑 JSON（缩写字段名）。"""
    try:
        mask = (df.index.date >= start_date) & (df.index.date <= end_date)  # type: ignore
        sliced = df[mask]
        result = []
        for idx, row in sliced.iterrows():
            result.append({
                'd': str(idx.date()),
                'o': round(float(row.get('open', 0)), 3),
                'h': round(float(row.get('high', 0)), 3),
                'l': round(float(row.get('low', 0)), 3),
                'c': round(float(row.get('close', 0)), 3),
                'v': int(row.get('volume', 0)),
            })
        return result
    except Exception:
        return []


def _safe_copy(d: dict) -> dict:
    """浅拷贝，去掉不可序列化字段。"""
    result = {}
    for k, v in d.items():
        try:
            json.dumps(v)
            result[k] = v
        except (TypeError, ValueError):
            result[k] = str(v)
    return result


# ─────────────────────────────────────────────
# 导入 / 导出
# ─────────────────────────────────────────────

# 导入记录的必填字段
_IMPORT_REQUIRED = {'code', 'name', 'direction', 'entry_price', 'created_at'}

# 导出时保留的字段（去掉内部快照等大字段）
_EXPORT_FIELDS = [
    'id', 'created_at', 'code', 'name', 'kind', 'direction',
    'entry_price', 'target_pct', 'stop_pct', 'horizon_days', 'deadline',
    'confidence', 'final_rating', 'final_action', 'user_suitability',
    'decision_view', 'strategy_tag', 'strategy_family', 'strategy_name',
    'strategy_stage', 'buy_strategy', 'strategy_reason',
    'sample_type', 'source',
    'status', 'grade', 'grade_detail', 'closed_at',
    'reasoning',
]


def import_predictions(
    records: list[dict],
    source_tag: str = 'imported',
    *,
    dedup: bool = True,
) -> dict:
    """导入外部预测记录到追踪系统。

    Args:
        records: 标准化预测记录列表，必填字段见 _IMPORT_REQUIRED
        source_tag: 来源标识（如 'imported:user_A', 'imported:prod_v3'）
        dedup: 按 code+created_at+direction 去重

    Returns:
        {'imported': n, 'skipped': n, 'errors': [str]}
    """
    tasks = load_tasks()
    existing_keys: set[str] = set()
    if dedup:
        for t in tasks:
            # 去重 key 含 source，不同来源的同标的同日预测各自独立
            k = f"{t.get('code')}_{t.get('created_at', '')[:10]}_{t.get('direction')}_{t.get('source', 'local')}"
            existing_keys.add(k)

    imported = 0
    skipped = 0
    errors: list[str] = []
    today = date.today()

    for i, rec in enumerate(records):
        missing = _IMPORT_REQUIRED - set(rec.keys())
        if missing:
            errors.append(f'#{i}: 缺少字段 {missing}')
            continue

        code = str(rec['code']).strip()
        name = str(rec['name']).strip()
        created = str(rec['created_at'])[:10]

        if dedup:
            key = f"{code}_{created}_{rec['direction']}_{source_tag}"
            if key in existing_keys:
                skipped += 1
                continue
            existing_keys.add(key)

        # 计算 deadline
        horizon = int(rec.get('horizon_days', 5) or 5)
        if rec.get('deadline'):
            deadline = str(rec['deadline'])
        else:
            try:
                from core.trade_calendar import add_trading_days
                d = date.fromisoformat(created)
                deadline = add_trading_days(horizon, d)
            except Exception:
                deadline = (date.fromisoformat(created) + timedelta(days=horizon)).isoformat()

        # 判断状态
        try:
            dl = date.fromisoformat(str(deadline)[:10])
            status = 'closed' if dl <= today else 'open'
        except Exception:
            status = 'open'

        task: dict[str, Any] = {
            'id': _next_task_id(created.replace('-', ''), tasks),
            'created_at': str(rec['created_at']),
            'kind': str(rec.get('kind', 'stock')),
            'final_rating': str(rec.get('final_rating', 'hold')),
            'intel_refs': [],
            'code': code,
            'name': name,
            'direction': rec['direction'],
            'entry_price': float(rec.get('entry_price') or 0),
            'target_pct': float(rec.get('target_pct') or 0),
            'stop_pct': float(rec.get('stop_pct') or 0),
            'horizon_days': horizon,
            'deadline': deadline,
            'confidence': float(rec.get('confidence', 0) or 0),
            'reasoning': str(rec.get('reasoning', '')),
            'pred_snapshot': rec,
            'intel_snapshot': [],
            'neutral_type': rec.get('neutral_type'),
            'expected_range_pct': rec.get('expected_range_pct'),
            'user_suitability': rec.get('user_suitability'),
            'final_action': rec.get('final_action'),
            'belief_snapshot': None,
            'strategy_tag': rec.get('strategy_tag', '未分类'),
            'risk_flags': [],
            'sample_type': 'watch',
            'source': source_tag,
            'error_type': None,
            'root_cause': None,
            'black_swan_flag': False,
            'status': status,
            'grade': rec.get('grade'),
            'grade_detail': rec.get('grade_detail'),
            'retrospective': None,
            'kline_at_close': None,
            'closed_at': rec.get('closed_at'),
        }
        tasks.append(task)
        imported += 1

    if imported > 0:
        save_tasks(tasks)
        append_tracking_event(
            'predictions_imported',
            source=source_tag,
            message='导入外部预测记录',
            payload={'record_count': imported, 'skipped': skipped, 'error_count': len(errors)},
        )
        logger.info('[tracking] 导入 %d 条预测记录 (source=%s)', imported, source_tag)

    return {'imported': imported, 'skipped': skipped, 'errors': errors}


def export_predictions(
    source: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """导出预测记录为标准化格式。

    Args:
        source: 按来源过滤（None=全部）
        status: 按状态过滤（'open'/'closed'/None=全部）

    Returns:
        [dict] 每条含 _EXPORT_FIELDS 中的字段
    """
    tasks = load_tasks()
    if source:
        tasks = [t for t in tasks if t.get('source', 'local') == source]
    if status:
        tasks = [t for t in tasks if t.get('status') == status]
    result = []
    for t in tasks:
        row = {k: t.get(k) for k in _EXPORT_FIELDS}
        result.append(row)
    return result


def export_predictions_rich(
    source: str | None = None,
    status: str | None = None,
) -> dict:
    """导出预测记录 + 统计摘要，买方可直接评估预测价值。

    导出格式（JSON）：
      {
        "meta": {"exported_at": "2026-05-24T...", "version": "2.0", ...},
        "stats": {...},     # get_tracking_stats 完整统计
        "records": [...]    # _EXPORT_FIELDS 记录列表
      }

    这是客户导出时用的接口 — 记录 + 统计一起给出，买方一目了然。
    """
    records = export_predictions(source=source, status=status)
    tasks = load_tasks()
    if source:
        tasks = [t for t in tasks if t.get('source', 'local') == source]
    if status:
        tasks = [t for t in tasks if t.get('status') == status]

    return {
        'meta': {
            'exported_at': datetime.now().isoformat(timespec='seconds'),
            'version': 'aldebaran_v3.0',
            'total_records': len(records),
            'source_filter': source,
            'status_filter': status,
        },
        'stats': get_tracking_stats(tasks),
        'records': records,
    }


_REPORT_FONT = 'Microsoft YaHei'


def _report_target_occupied_message(target: Path) -> str:
    return (
        f'目标文件正在被占用，无法覆盖导出：{target}\n'
        '请先关闭 Word 中打开的旧报告，或选择“另存为”到新文件后再导出。'
    )


def ensure_report_target_writable(target_path: str | Path) -> None:
    """导出前检查目标 Word 文件是否可覆盖，避免 AI 分析后才失败。"""
    target = Path(target_path)
    if not target.exists():
        return
    try:
        with target.open('r+b'):
            pass
    except PermissionError as e:
        raise RuntimeError(_report_target_occupied_message(target)) from e


def _set_east_asia_font(rpr) -> None:
    try:
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        rfonts = rpr.rFonts
        if rfonts is None:
            rfonts = OxmlElement('w:rFonts')
            rpr.append(rfonts)
        rfonts.set(qn('w:eastAsia'), _REPORT_FONT)
        rfonts.set(qn('w:ascii'), _REPORT_FONT)
        rfonts.set(qn('w:hAnsi'), _REPORT_FONT)
    except Exception:
        return


def _doc_set_style_font(doc, style_name: str, size_pt: float, *, bold: bool = False, after_pt: float = 4) -> None:
    from docx.shared import Pt

    style = doc.styles[style_name]
    style.font.name = _REPORT_FONT
    style.font.size = Pt(size_pt)
    style.font.bold = bold
    _set_east_asia_font(style._element.get_or_add_rPr())
    style.paragraph_format.space_after = Pt(after_pt)


def _doc_apply_report_styles(doc) -> None:
    """统一 Word 报告基础字体，避免不同机器上的默认样式忽大忽小。"""
    from docx.shared import Pt

    _doc_set_style_font(doc, 'Normal', 10.5, bold=False, after_pt=5)
    _doc_set_style_font(doc, 'Title', 18, bold=True, after_pt=8)
    _doc_set_style_font(doc, 'Heading 1', 13, bold=True, after_pt=5)
    _doc_set_style_font(doc, 'Heading 2', 11.5, bold=True, after_pt=4)
    doc.styles['Normal'].paragraph_format.line_spacing = 1.08
    for section in doc.sections:
        section.top_margin = Pt(54)
        section.bottom_margin = Pt(54)
        section.left_margin = Pt(54)
        section.right_margin = Pt(54)


def _doc_set_run_font(run, size_pt: float, *, bold: bool | None = None) -> None:
    from docx.shared import Pt

    run.font.name = _REPORT_FONT
    run.font.size = Pt(size_pt)
    if bold is not None:
        run.bold = bold
    _set_east_asia_font(run._element.get_or_add_rPr())


def _doc_format_table(table) -> None:
    table.style = 'Table Grid'
    for row_idx, row in enumerate(table.rows):
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    _doc_set_run_font(run, 9.5, bold=(row_idx == 0))


def _doc_add_kv_table(doc, rows: list[tuple[str, Any]]) -> None:
    table = doc.add_table(rows=0, cols=2)
    for key, value in rows:
        cells = table.add_row().cells
        cells[0].text = str(key)
        cells[1].text = '' if value is None else str(value)
    _doc_format_table(table)


def _doc_add_group_table(doc, data: dict, title_col: str) -> None:
    table = doc.add_table(rows=1, cols=7)
    headers = [title_col, '样本数', '已结算', '有效率口径', '有效率', '平均R', '典型问题']
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    if not data:
        cells = table.add_row().cells
        cells[0].text = '样本不足'
        _doc_format_table(table)
        return
    for key, row in list(data.items())[:12]:
        cells = table.add_row().cells
        cells[0].text = _cn_report_label(key)
        cells[1].text = str(row.get('sample_count', 0))
        cells[2].text = str(row.get('closed_count', 0))
        cells[3].text = str(row.get('win_rate_basis') or '样本不足')
        cells[4].text = str(row.get('win_rate_label', '样本不足'))
        cells[5].text = str(row.get('avg_r_label') or '样本不足')
        cells[6].text = '、'.join(row.get('typical_problems') or ['样本不足'])
    _doc_format_table(table)


def _grade_count_text(counts: dict) -> str:
    if not counts:
        return '样本不足'
    order = ['A', 'B', 'C', 'D', 'E', 'F', 'α', 'β', 'γ', 'δ']
    parts = [f'{g}:{counts.get(g)}' for g in order if counts.get(g)]
    parts.extend(f'{g}:{v}' for g, v in counts.items() if g not in order and v)
    return ' '.join(parts) if parts else '样本不足'


def _doc_add_scope_table(doc, data: dict) -> None:
    table = doc.add_table(rows=1, cols=9)
    headers = ['层级', '样本', '已结算', '评级分布', '有效率', '平均涨跌%', 'R覆盖', '平均/中位R', '主要拖后腿']
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    if not data:
        table.add_row().cells[0].text = '样本不足'
        _doc_format_table(table)
        return
    ordered = list(_SCOPE_LABELS.keys()) + [k for k in data.keys() if k not in _SCOPE_LABELS]
    for scope in ordered:
        row = data.get(scope)
        if not row:
            continue
        cells = table.add_row().cells
        cells[0].text = str(row.get('label') or _scope_label(scope))
        cells[1].text = str(row.get('sample_count', 0))
        cells[2].text = str(row.get('closed_count', 0))
        cells[3].text = _grade_count_text(row.get('grade_counts') or {})
        cells[4].text = str(row.get('win_rate_label') or '样本不足')
        cells[5].text = str(row.get('avg_return_pct') if row.get('avg_return_pct') is not None else '样本不足')
        cells[6].text = f'{row.get("r_count", 0)}/{row.get("closed_count", 0)}'
        cells[7].text = f'{row.get("avg_r_label") or "样本不足"} / {row.get("median_r_label") or "样本不足"}'
        drags = row.get('drag_factors') or []
        cells[8].text = '；'.join(f'{d.get("title")}({d.get("count", 0)})' for d in drags) or '暂无明显拖后腿'
    _doc_format_table(table)


def _doc_add_layered_diagnostics(doc, diagnostics: list[dict]) -> None:
    if not diagnostics:
        doc.add_paragraph('样本不足，暂未形成分层拖后腿诊断。')
        return
    for item in diagnostics:
        ids = ', '.join((item.get('sample_ids') or [])[:5]) or '无代表样本'
        doc.add_paragraph(
            f'{item.get("layer")}｜{item.get("title")}：{item.get("count", 0)} 个样本；'
            f'代表样本：{ids}；建议：{item.get("suggestion") or "按分层口径复核"}'
        )


def _doc_add_records_table(doc, records: list[dict]) -> None:
    table = doc.add_table(rows=1, cols=10)
    headers = ['ID', '代码', '名称', '样本', '方向倾向', '模式', '评级', '最终%', 'R倍数', '动作说明']
    for i, h in enumerate(headers):
        table.rows[0].cells[i].text = h
    if not records:
        cells = table.add_row().cells
        cells[0].text = '暂无逐笔记录'
        _doc_format_table(table)
        return
    for rec in records[:80]:
        cells = table.add_row().cells
        cells[0].text = str(rec.get('id') or '')
        cells[1].text = str(rec.get('code') or '')
        cells[2].text = str(rec.get('name') or '')
        cells[3].text = _scope_label(rec.get('evaluation_scope')) if rec.get('evaluation_scope') else '观察单'
        cells[4].text = str(rec.get('bias_label') or '')
        cells[5].text = str(rec.get('setup') or '')
        cells[6].text = str(rec.get('grade') or '')
        cells[7].text = str(rec.get('final_pct') if rec.get('final_pct') is not None else '')
        cells[8].text = str(rec.get('r_multiple') if rec.get('r_multiple') is not None else rec.get('r_missing_reason') or '')
        cells[9].text = str(rec.get('action_label') or '')
    _doc_format_table(table)


def _cn_option(value: Any) -> str:
    mapping = {
        'all': '全部',
        'trade': '观察单',
        'watch': '观察单',
        'stock': '个股',
        'etf': 'ETF',
        'sector': '板块',
        'high': '高',
        'medium': '中',
        'low': '低',
    }
    return mapping.get(str(value), str(value))


def _cn_event_type(value: Any) -> str:
    mapping = {
        'task_created': '创建追踪任务',
        'task_settled': '追踪任务结算',
        'data_anomaly': '数据异常',
        'retrospective_started': '反思开始',
        'retrospective_completed': '反思完成',
        'retrospective_failed': '反思失败',
        'predictions_imported': '预测导入',
        'json_exported': 'JSON导出',
        'report_exported': 'Word报告导出',
        'report_export_failed': 'Word报告导出失败',
        'report_ai_summary_failed': 'AI总评失败',
    }
    return mapping.get(str(value), str(value))


def _case_rows(records: list[dict], success: bool) -> list[dict]:
    summary = _build_case_summary(records)
    return summary['success' if success else 'failure']


def _generate_report_ai_summary(report: dict, api_key: str = '') -> str:
    if not api_key:
        from core.credentials import load_api_key
        api_key = load_api_key() or ''
    if not api_key:
        raise RuntimeError('缺少 API Key')
    from core.agents.base import call_pro
    system = '你是投资系统复盘助手，只做系统性偏差分析，不给具体买卖推荐。'
    prompt = (
        '请基于以下追踪报告数据生成 500-800 字中文总评，必须包含：'
        '系统性偏差、模式升降权建议、Agent 数据边界、下次前三个修改目标。'
        '不得给具体买卖推荐。\n\n'
        + json.dumps({
            'meta': report.get('meta'),
            'summary': report.get('summary'),
            'performance_by_setup': report.get('performance_by_setup'),
            'performance_by_agent': report.get('performance_by_agent'),
            'risk_diagnostics': report.get('risk_diagnostics'),
            'error_analysis': report.get('error_analysis'),
            'improvement_candidates': report.get('improvement_candidates'),
        }, ensure_ascii=False, default=str)
    )
    content, _reasoning = call_pro(system, prompt, api_key, max_tokens=1600, temperature=0.3)
    return str(content or '').strip() or 'AI 总评生成失败：返回内容为空'


def export_tracking_report_docx(
    target_path: str | Path,
    options: dict | None = None,
    *,
    include_ai_summary: bool = False,
    api_key: str = '',
) -> dict:
    """导出 Word 复盘报告；默认只生成确定性总结。"""
    target = Path(target_path)
    try:
        ensure_report_target_writable(target)
    except RuntimeError as e:
        append_tracking_event(
            'report_export_failed',
            severity='warning',
            message='Word 报告目标文件被占用',
            payload={'error': str(e), 'file_type': 'docx', 'path': str(target)},
        )
        raise
    try:
        from docx import Document
    except Exception as e:
        append_tracking_event(
            'report_export_failed',
            severity='error',
            message='缺少 python-docx，无法导出 Word 复盘报告',
            payload={'error': str(e), 'file_type': 'docx'},
        )
        raise RuntimeError('缺少 python-docx 依赖，请先安装 python-docx>=1.1') from e

    report = build_tracking_report_data(options)
    ai_summary = ''
    if include_ai_summary:
        try:
            ai_summary = _generate_report_ai_summary(report, api_key)
        except Exception as e:
            ai_summary = f'AI 总评生成失败：{e}'
            append_tracking_event(
                'report_ai_summary_failed',
                severity='warning',
                message='AI 总评生成失败，Word 报告继续导出',
                payload={'error': str(e)},
            )

    doc = Document()
    _doc_apply_report_styles(doc)
    meta = report['meta']
    summary = report['summary']
    doc.add_heading('追踪复盘报告', 0)
    range_label = '全部' if meta['range_days'] == 'all' else f'最近{meta["range_days"]}天'
    doc.add_paragraph(
        f'报告范围：{range_label}；'
        f'样本范围：{_cn_option(meta["sample_scope"])}；'
        f'标的范围：{_cn_option(meta["kind_scope"])}'
    )
    doc.add_paragraph(f'生成时间：{meta["generated_at"]}')
    doc.add_paragraph('免责声明：本报告用于复盘系统预测质量和流程改进，不构成任何投资建议，不用于自动交易。')

    doc.add_heading('总览', level=1)
    _doc_add_kv_table(doc, [
        ('样本数', meta.get('sample_count')),
        ('已结算样本', meta.get('closed_count')),
        ('观察单数量', summary.get('watch_count')),
        ('提醒日志数量', meta.get('alert_log_count')),
        ('分析记忆条数', (meta.get('analysis_memory_stats') or {}).get('blocks')),
        ('方向有效率', summary.get('win_rate_label')),
        ('达标率', summary.get('target_rate_label')),
        ('平均真实涨跌%', summary.get('avg_return_pct') if summary.get('avg_return_pct') is not None else '样本不足'),
        ('平均R', summary.get('avg_r_label') or '样本不足'),
        ('方向观察样本', (summary.get('direction_watch_stats') or {}).get('total', 0)),
        ('方向观察准确率', (summary.get('direction_watch_stats') or {}).get('accuracy_label', '样本不足')),
    ])
    doc.add_heading('数据状态说明', level=1)
    notes = report.get('data_quality_notes') or []
    if notes:
        for note in notes:
            doc.add_paragraph(note)
    else:
        doc.add_paragraph('暂无明显数据质量限制。')

    doc.add_heading('分层复盘口径', level=1)
    doc.add_paragraph(
        '本系统追踪记录均为虚拟观察单；以下分层把模拟交易、偏多未触发、中性观察和回避观察拆开，'
        '避免把无 R 的观察样本解释成实盘收益。'
    )
    _doc_add_scope_table(doc, report.get('performance_by_scope') or {})

    doc.add_heading('模式表现', level=1)
    _doc_add_group_table(doc, report.get('performance_by_setup') or {}, '模式')

    doc.add_heading('机会等级表现', level=1)
    _doc_add_group_table(doc, report.get('performance_by_opportunity') or {}, '机会等级')

    doc.add_heading('Agent 贡献分析', level=1)
    for label, row in (report.get('performance_by_agent') or {}).items():
        doc.add_paragraph(
            f'{label}：样本 {row.get("sample_count", 0)}，命中 {row.get("hit", 0)}，'
            f'误导 {row.get("misleading", 0)}，状态 {row.get("status", "样本不足")}'
        )
    agg = report.get('aggregator_insight') or {}
    agg_text = agg.get('text')
    if agg_text:
        doc.add_paragraph('')
        p = doc.add_paragraph()
        run = p.add_run('★ ')
        run.bold = True
        p.add_run(agg_text)

    doc.add_heading('风险诊断', level=1)
    if report.get('risk_diagnostics'):
        for label, row in report['risk_diagnostics'].items():
            doc.add_paragraph(f'{label}：{row.get("count", 0)} 次；样本 {", ".join(row.get("sample_ids", []))}')
    else:
        doc.add_paragraph('样本不足，暂未发现稳定风险标签。')

    doc.add_heading('层级拖后腿诊断', level=1)
    _doc_add_layered_diagnostics(doc, report.get('layered_diagnostics') or [])

    doc.add_heading('模拟交易强成功案例 Top 5', level=1)
    case_summary = report.get('case_summary') or {}
    strong_cases = case_summary.get('strong_success') or []
    if strong_cases:
        for rec in strong_cases:
            r_text = rec.get('r_multiple') if rec.get('r_multiple') is not None else rec.get('r_missing_reason') or '未计算'
            doc.add_paragraph(
                f'{rec.get("id")} {rec.get("name")}：评级 {rec.get("grade")}，'
                f'R={r_text}，条件：{_cn_report_label(rec.get("setup"))}'
            )
    else:
        doc.add_paragraph('暂无强成功案例（需 A/B 评级且 R>1.5 或回报>3%）。')

    doc.add_heading('观察踏空/未转入场案例 Top 5', level=1)
    missed_cases = case_summary.get('missed_opportunity') or []
    if missed_cases:
        for rec in missed_cases:
            doc.add_paragraph(
                f'{rec.get("id")} {rec.get("name")}：评级 {rec.get("grade")}，'
                f'最终涨跌={rec.get("final_pct")}%，层级={_scope_label(rec.get("evaluation_scope"))}，'
                f'动作：{rec.get("action_label") or "待复核"}'
            )
    else:
        doc.add_paragraph('暂无明显观察踏空样本。')

    doc.add_heading('达标案例 Top 5', level=1)
    qualified_cases = case_summary.get('qualified_success') or []
    if qualified_cases:
        for rec in qualified_cases:
            r_text = rec.get('r_multiple') if rec.get('r_multiple') is not None else rec.get('r_missing_reason') or '未计算'
            doc.add_paragraph(
                f'{rec.get("id")} {rec.get("name")}：评级 {rec.get("grade")}，'
                f'R={r_text}，条件：{_cn_report_label(rec.get("setup"))}'
            )
    else:
        doc.add_paragraph(
            '暂无达标案例。C/γ 等中性样本不会被默认包装为成功，需正收益或明确成功避险证据。'
        )

    doc.add_heading('失败案例 Top 5', level=1)
    failure_cases = case_summary.get('failure') or []
    if failure_cases:
        for rec in failure_cases:
            r_text = rec.get('r_multiple') if rec.get('r_multiple') is not None else rec.get('r_missing_reason') or '未计算'
            doc.add_paragraph(
                f'{rec.get("id")} {rec.get("name")}：评级 {rec.get("grade")}，'
                f'R={r_text}，归因：{rec.get("root_cause") or "待复盘"}'
            )
    else:
        doc.add_paragraph('暂无明确失败案例。')

    doc.add_heading('系统性改进建议', level=1)
    candidates = report.get('improvement_candidates') or []
    if candidates:
        for item in candidates:
            doc.add_paragraph(
                f'{item.get("title")}（严重度：{_cn_option(item.get("severity"))}）：{item.get("evidence")}；'
                f'建议：{item.get("suggestion")}；位置：{item.get("suggested_location")}'
            )
    else:
        doc.add_paragraph('样本不足，暂不生成系统性改进候选。')

    if include_ai_summary:
        doc.add_heading('AI 总评', level=1)
        doc.add_paragraph(ai_summary or 'AI 总评生成失败：返回内容为空')

    doc.add_heading('AI 二次分析提示词', level=1)
    doc.add_paragraph(report.get('ai_prompt') or '')

    doc.add_heading('附录：逐笔明细', level=1)
    _doc_add_records_table(doc, report.get('records') or [])

    doc.add_heading('附录：审计日志摘要', level=1)
    audit = report.get('audit_log_summary') or {}
    doc.add_paragraph(f'审计日志总数：{audit.get("total_events", 0)}')
    for et, count in (audit.get('by_type') or {}).items():
        doc.add_paragraph(f'{_cn_event_type(et)}：{count}')

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        doc.save(str(target))
    except PermissionError as e:
        message = _report_target_occupied_message(target)
        append_tracking_event(
            'report_export_failed',
            severity='warning',
            message='Word 报告目标文件被占用',
            payload={'error': str(e), 'file_type': 'docx', 'path': str(target)},
        )
        raise RuntimeError(message) from e
    save_improvement_candidates(candidates)
    append_tracking_event(
        'report_exported',
        message='导出 Word 复盘报告',
        payload={
            'file_type': 'docx',
            'path': str(target),
            'record_count': meta.get('sample_count', 0),
            'candidate_count': len(candidates),
            'include_ai_summary': include_ai_summary,
        },
    )
    return {
        'path': str(target),
        'record_count': meta.get('sample_count', 0),
        'candidate_count': len(candidates),
    }


def delete_task(task_id: str, *, reason: str = '用户手动删除') -> dict:
    """硬删除一条追踪任务；被删任务不再参与胜率、报告和导出统计。"""
    tasks = load_tasks()
    for i, t in enumerate(tasks):
        if t.get('id') == task_id:
            summary = _delete_summary(t, reason)
            tasks.pop(i)
            save_tasks(tasks)
            append_tracking_event(
                'task_deleted',
                task_id=task_id,
                code=summary.get('code'),
                name=summary.get('name'),
                source=t.get('source', 'local'),
                message=f'用户删除追踪任务: {summary.get("name")} ({summary.get("code")})',
                payload=summary,
            )
            return {'deleted': True, 'task': summary}
    return {'deleted': False, 'reason': 'not_found'}


def _delete_summary(task: dict, reason: str) -> dict:
    return {
        'task_id': task.get('id'),
        'code': task.get('code'),
        'name': task.get('name'),
        'status': task.get('status'),
        'grade': task.get('grade'),
        'final_pct': _float_or_none((task.get('grade_detail') or {}).get('final_pct')),
        'sample_type': task.get('sample_type'),
        'reason': reason,
    }


def delete_tasks(task_ids: list[str], *, reason: str = '用户批量删除') -> dict:
    """批量硬删除追踪任务；只读写任务文件一次，避免 UI 长时间卡住。"""
    ids = [str(x) for x in dict.fromkeys(task_ids or []) if str(x)]
    if not ids:
        return {'deleted': 0, 'tasks': [], 'missing': []}
    wanted = set(ids)
    tasks = load_tasks()
    kept: list[dict] = []
    deleted: list[dict] = []
    found: set[str] = set()
    for task in tasks:
        task_id = str(task.get('id') or '')
        if task_id in wanted:
            deleted.append(_delete_summary(task, reason))
            found.add(task_id)
        else:
            kept.append(task)
    if deleted:
        save_tasks(kept)
        append_tracking_event(
            'tasks_deleted',
            message=f'用户批量删除追踪任务: {len(deleted)} 条',
            payload={'tasks': deleted, 'reason': reason, 'count': len(deleted)},
        )
    return {'deleted': len(deleted), 'tasks': deleted, 'missing': [x for x in ids if x not in found]}


def list_sources() -> list[dict]:
    """列出所有来源及其任务数量。"""
    tasks = load_tasks()
    counts: dict[str, dict[str, int]] = {}
    for t in tasks:
        src = t.get('source', 'local')
        row = counts.setdefault(src, {'total': 0, 'open': 0, 'closed': 0, 'graded': 0})
        row['total'] += 1
        if t.get('status') == 'open':
            row['open'] += 1
        elif t.get('status') == 'closed':
            row['closed'] += 1
            if t.get('grade'):
                row['graded'] += 1
    return [{'source': src, **v} for src, v in sorted(counts.items())]


__all__ = [
    'load_tasks', 'save_tasks', 'get_open_tasks', 'get_closed_tasks',
    'append_tracking_event', 'load_tracking_events',
    'create_task', 'try_settle_due_tasks', 'run_tracking_maintenance',
    'backfill_triggered_watch_tasks',
    'pending_maintenance_ids', 'score_task',
    'get_tracking_stats', 'benchmark_index_df', 'build_tracking_report_data', 'build_retrospective_prompt',
    'import_predictions', 'export_predictions', 'export_predictions_rich',
    'export_tracking_report_docx', 'ensure_report_target_writable',
    'save_improvement_candidates', 'delete_task', 'delete_tasks', 'list_sources',
]
