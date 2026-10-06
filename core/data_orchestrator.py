"""数据编排层：并发拉取 K线/财务/资金流，统一返回 StockContext。

双层缓存设计：
  推理层：CACHE_DIR/stock_context/{code}.json  4h 有效
          含 kline_summary / fundamental_summary / money_flow_summary（文字）
  数据层：每次调用 refresh_realtime() 实时刷新 current_price / today_main_flow
          复用 get_stock_quotes() 统一行情入口，零 AI 成本，不写盘

StockContext 字段：
  kline_summary         str   K线技术指标摘要（prompt 注入用）
  fundamental_summary   str   基本面摘要（prompt 注入用）
  money_flow_summary    str   资金流摘要（prompt 注入用）
  realtime              dict  {current_price, today_pct, main_flow, xlarge_flow}
  code                  str
  name                  str
  cached_at             str   ISO 时间，供 UI 显示"上次分析 XX 分钟前"
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from core.paths import CACHE_DIR as _BASE_CACHE
_CACHE_DIR = _BASE_CACHE / 'stock_context'
_CONTEXT_TTL_SECONDS = 4 * 3600  # 非交易时段最多4小时
_CONTEXT_ACTIVE_TTL_SECONDS = 5 * 60  # 交易时段最多5分钟
logger = logging.getLogger(__name__)


def _cache_path(code: str) -> Path:
    return _CACHE_DIR / f'{code}.json'


def _context_is_fresh(code: str) -> bool:
    p = _cache_path(code)
    if not p.exists():
        return False
    try:
        meta = json.loads(p.read_text(encoding='utf-8'))
        cached_at_str = meta.get('cached_at', '')
        if not cached_at_str:
            return False
        cached_at = datetime.fromisoformat(cached_at_str)
        now = datetime.now()
        delta = (now - cached_at).total_seconds()
        try:
            from core.trade_calendar import is_trade_day
            hm = now.hour * 60 + now.minute
            active = bool(is_trade_day(now.date()) and ((565 <= hm <= 695) or (775 <= hm <= 905)))
        except Exception:
            active = now.weekday() < 5 and 9 <= now.hour <= 15
        ttl = _CONTEXT_ACTIVE_TTL_SECONDS if active else _CONTEXT_TTL_SECONDS
        if delta >= ttl:
            return False
        try:
            from core.kline_provider import _latest_available_trade_date
            expected = _latest_available_trade_date()
            tech = meta.get('technical_profile') or {}
            kline_date = str(tech.get('asof') or '')[:10]
            flow = meta.get('flow_profile') or {}
            flow_date = str((flow.get('last') or {}).get('date') or '')[:10]
            if kline_date and kline_date < expected:
                return False
            if flow_date and flow_date < expected:
                return False
        except Exception:
            pass
        return True
    except Exception:
        return False


def _load_context(code: str) -> dict | None:
    try:
        return json.loads(_cache_path(code).read_text(encoding='utf-8'))
    except Exception:
        return None


def _save_context(code: str, ctx: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(code).write_text(
        json.dumps(ctx, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8',
    )


def _fetch_kline_summary(
    code: str,
    name: str | None = None,
    flow_profile: dict | None = None,
    public_evidence: dict | None = None,
    market_phase: str | None = None,
) -> tuple[str, float | None, dict | None]:
    """返回 (summary_text, 最后一根close价格)。"""
    try:
        from core.kline_provider import fetch_daily_kline, summarize, build_technical_profile
        df = fetch_daily_kline(code)
        last_close = float(df['close'].iloc[-1]) if df is not None and not df.empty else None
        return summarize(code, df=df), last_close, build_technical_profile(
            code, df=df, name=name,
            flow_profile=flow_profile,
            public_evidence=public_evidence,
            market_phase=market_phase,
        )
    except Exception as e:
        logger.warning('[data_orchestrator] K线刷新失败 code=%s', code, exc_info=True)
        return f'▶ K 线数据：获取失败（{e}）', None, None


def _fetch_fundamental_summary(code: str) -> str:
    try:
        from core.fundamentals_provider import summarize
        return summarize(code)
    except Exception as e:
        return f'▶ 基本面：获取失败（{e}）'


def _fetch_money_flow(code: str, name: str | None = None) -> tuple[str, dict]:
    try:
        from core.money_flow_provider import summarize
        return summarize(code, name=name)
    except Exception as e:
        logger.warning('[data_orchestrator] 资金流刷新失败 code=%s source=money_flow_provider', code, exc_info=True)
        return f'▶ 资金流向：获取失败（{e}）', {}


def _fetch_public_evidence(code: str, name: str | None = None) -> dict | None:
    try:
        from core.public_fund_evidence import get_public_evidence
        return get_public_evidence(code, name=name)
    except Exception:
        return None


def _fetch_restricted_release(code: str) -> dict | None:
    try:
        from core.restricted_release_provider import fetch_restricted_release
        return fetch_restricted_release(code)
    except Exception:
        return None


def _fetch_hot_rank(code: str) -> dict | None:
    code_6 = str(code).strip()[-6:]
    # 主源：akshare
    try:
        import akshare as ak
        df = ak.stock_hot_rank_em()
        if df is not None and not df.empty:
            code_col = next((c for c in df.columns if '代码' in c), None)
            if code_col:
                mask = df[code_col].astype(str).str.strip().str[-6:] == code_6
                sub = df[mask]
                if not sub.empty:
                    row = sub.iloc[0]
                    rank_col = next((c for c in df.columns if ('序号' in c or '排名' in c) and '变化' not in c), None)
                    rank = int(row[rank_col]) if rank_col else 0
                    change_col = next((c for c in df.columns if '变化' in c), None)
                    change = str(row[change_col]).strip() if change_col else ''
                    return {'rank': rank, 'change': change}
    except Exception:
        logger.debug('[data_orchestrator] 热度 akshare 刷新失败 code=%s', code_6, exc_info=True)
    # 兜底：HTTP 直连
    try:
        from core.astock_http_provider import fetch_hot_rank
        for item in fetch_hot_rank():
            if str(item.get('代码', '')).strip()[-6:] == code_6:
                return {'rank': item.get('排名', 0), 'change': str(item.get('变化', ''))}
    except Exception:
        logger.debug('[data_orchestrator] 热度 HTTP 兜底失败 code=%s', code_6, exc_info=True)
    return None


def _refresh_market_context(ctx: dict):
    """快速（非阻塞缓存命中）更新 emotion/global，走各自短 TTL 缓存。"""
    try:
        from core.market_context_provider import get_emotion_snapshot, get_global_snapshot, get_market_phase
        ctx['emotion'] = get_emotion_snapshot()
        ctx['global'] = get_global_snapshot()
        ctx['market_phase'] = get_market_phase(ctx.get('emotion'))
    except Exception:
        logger.debug('[data_orchestrator] 市场上下文刷新失败', exc_info=True)


def gather_stock_context(code: str, name: str, force_refresh: bool = False) -> dict:
    """并发拉取 K线/财务/资金流，返回 StockContext dict。

    若推理层缓存 4h 内有效且 force_refresh=False，直接返回缓存（仍会刷新 realtime）。
    """
    if not force_refresh and _context_is_fresh(code):
        ctx = _load_context(code)
        if ctx:
            dirty = False
            tech_profile = ctx.get('technical_profile') or {}
            kline_bad = '获取失败' in str(ctx.get('kline_summary') or '')
            tech_missing_ma40 = bool(tech_profile.get('available') and not (tech_profile.get('ma') or {}).get('ma40'))
            if not ctx.get('flow_profile'):
                try:
                    from core.money_flow_provider import build_flow_profile
                    ctx['flow_profile'] = build_flow_profile(code)
                    dirty = True
                except Exception:
                    logger.debug(
                        '[data_orchestrator] flow_profile 刷新失败 code=%s',
                        code,
                        exc_info=True,
                    )
            if ctx.get('public_fund_evidence') is None:
                ctx['public_fund_evidence'] = _fetch_public_evidence(code, name)
                dirty = True
            _refresh_market_context(ctx)
            if (not tech_profile) or (not tech_profile.get('available')) or tech_missing_ma40 or kline_bad or not tech_profile.get('behavior_tags'):
                try:
                    kline_text, kline_close, rebuilt_profile = _fetch_kline_summary(
                        code, name=name,
                        flow_profile=ctx.get('flow_profile'),
                        public_evidence=ctx.get('public_fund_evidence'),
                        market_phase=ctx.get('market_phase'),
                    )
                    ctx['kline_summary'] = kline_text
                    ctx['_kline_last_close'] = kline_close
                    ctx['technical_profile'] = rebuilt_profile
                    dirty = True
                except Exception:
                    logger.warning(
                        '[data_orchestrator] 缓存 K线重建失败 code=%s',
                        code,
                        exc_info=True,
                    )
            if ctx.get('restricted_release') is None:
                ctx['restricted_release'] = _fetch_restricted_release(code)
                dirty = True
            if ctx.get('hot_rank') is None:
                ctx['hot_rank'] = _fetch_hot_rank(code)
                dirty = True
            refresh_realtime(ctx)
            if dirty:
                _save_context(code, ctx)
            return ctx

    # ── 5 路数据源并发拉取（emotion/global 独立短 TTL，不入 stock_context 缓存） ──
    results: dict = {
        'kline_summary': '',
        'fundamental_summary': '',
        'money_flow_summary': '',
        'money_flow_realtime': {},
        'flow_profile': None,
        'technical_profile': None,
        'emotion': None,
        'global': None,
        'market_phase': None,
        'public_fund_evidence': None,
        'restricted_release': None,
        'hot_rank': None,
    }

    def _run(task_name: str, fn, *args):
        return task_name, fn(*args)

    def _fetch_emotion():
        from core.market_context_provider import get_emotion_snapshot
        return get_emotion_snapshot()

    def _fetch_global():
        from core.market_context_provider import get_global_snapshot
        return get_global_snapshot()

    tasks = [
        ('fundamentals', _fetch_fundamental_summary, code),
        ('money_flow', _fetch_money_flow, code, name),
        ('public_fund_evidence', _fetch_public_evidence, code, name),
        ('emotion', _fetch_emotion),
        ('global', _fetch_global),
        ('restricted_release', _fetch_restricted_release, code),
        ('hot_rank', _fetch_hot_rank, code),
    ]

    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {
            executor.submit(_run, t[0], t[1], *t[2:]): t[0]
            for t in tasks
        }
        for future in as_completed(futures):
            try:
                task_name, result = future.result(timeout=20)
                if task_name == 'fundamentals':
                    results['fundamental_summary'] = result
                elif task_name == 'money_flow':
                    summary, realtime = result
                    results['money_flow_summary'] = summary
                    results['money_flow_realtime'] = realtime
                    results['flow_profile'] = (realtime or {}).get('flow_profile')
                elif task_name == 'public_fund_evidence':
                    results['public_fund_evidence'] = result
                elif task_name == 'emotion':
                    results['emotion'] = result
                elif task_name == 'global':
                    results['global'] = result
                elif task_name == 'restricted_release':
                    results['restricted_release'] = result
                elif task_name == 'hot_rank':
                    results['hot_rank'] = result
            except Exception:
                task_name = futures.get(future, 'unknown')
                logger.warning(
                    '[data_orchestrator] 并发任务失败 code=%s task=%s',
                    code,
                    task_name,
                    exc_info=True,
                )

    try:
        from core.market_context_provider import get_market_phase
        results['market_phase'] = get_market_phase(results.get('emotion'))
    except Exception:
        results['market_phase'] = 'unknown'

    kline_text, kline_close, tech_profile = _fetch_kline_summary(
        code, name=name,
        flow_profile=results.get('flow_profile'),
        public_evidence=results.get('public_fund_evidence'),
        market_phase=results.get('market_phase'),
    )
    results['kline_summary'] = kline_text
    results['kline_last_close'] = kline_close
    results['technical_profile'] = tech_profile

    now = datetime.now().isoformat()
    # stock_context 缓存：仅含 K线/财务/资金流（4h TTL）
    ctx = {
        'code': code,
        'name': name,
        '_kline_last_close': results.get('kline_last_close'),  # 价格交叉校验用
        'kline_summary': results['kline_summary'],
        'technical_profile': results['technical_profile'],
        'fundamental_summary': results['fundamental_summary'],
        'money_flow_summary': results['money_flow_summary'],
        'flow_profile': results['flow_profile'],
        'realtime': results['money_flow_realtime'],
        'cached_at': now,
        # emotion/global 附带但不参与 4h 缓存命中判断（每次都重新拉）
        'emotion': results['emotion'],
        'global': results['global'],
        'market_phase': results['market_phase'],
        'public_fund_evidence': results['public_fund_evidence'],
        'restricted_release': results['restricted_release'],
        'hot_rank': results['hot_rank'],
    }
    refresh_realtime(ctx)
    _save_context(code, ctx)
    return ctx


def refresh_realtime(ctx: dict) -> dict:
    """实时刷新价格/资金流快照（不写盘，零 AI 成本）。

    更新 ctx['realtime'] 中的 current_price / today_pct / main_flow / xlarge_flow。
    若 K 线末根 close 与实时价偏离 > 35%，强制用实时价并写 _price_source='quote_override'。
    """
    code = ctx.get('code', '')
    if not code:
        return ctx

    realtime = dict(ctx.get('realtime') or {})
    routed_price: float | None = None

    try:
        from core.data_source import get_stock_quotes
        quotes = get_stock_quotes([code])
        q = quotes.get(code, {})
        if q:
            routed_price = q.get('price')
            realtime['current_price'] = routed_price
            realtime['today_pct'] = q.get('pct')
            realtime['_price_source'] = q.get('source') or 'routed'
    except Exception:
        logger.warning(
            '[data_orchestrator] 实时价格刷新失败 code=%s source=quotes_routed',
            code,
            exc_info=True,
        )

    # 交叉校验：若 K 线 close 与实时价偏离 > 35%，标记警告（实时价已经是优先的）
    kline_close = ctx.get('_kline_last_close')
    if routed_price and kline_close and kline_close > 0 and routed_price > 0:
        ratio = abs(kline_close - routed_price) / routed_price
        if ratio > 0.35:
            realtime['_price_warning'] = (
                f'K线末根close={kline_close:.3f}与实时价{routed_price:.3f}'
                f'偏离{ratio*100:.1f}%（>35%），已用实时价'
            )
            realtime['_price_source'] = 'quote_override'

    try:
        from core.money_flow_provider import fetch_today_flow
        flow = fetch_today_flow(code)
        if flow:
            realtime['main_flow'] = flow.get('main')
            realtime['xlarge_flow'] = flow.get('xlarge')
    except Exception:
        logger.warning(
            '[data_orchestrator] 实时资金流刷新失败 code=%s source=money_flow_provider',
            code,
            exc_info=True,
        )

    ctx['realtime'] = realtime
    ctx['realtime_at'] = datetime.now().isoformat()
    return ctx


def minutes_since_cached(ctx: dict) -> int | None:
    """返回推理层缓存距今多少分钟，用于 UI 提示。"""
    try:
        cached_at = datetime.fromisoformat(ctx.get('cached_at', ''))
        return int((datetime.now() - cached_at).total_seconds() / 60)
    except Exception:
        return None
