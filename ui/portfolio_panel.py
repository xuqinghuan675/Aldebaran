"""持仓 + 预测面板（v3.0 三段式垂直布局）。

布局：
  ① 顶栏 32px：添加持仓
  ② 持仓表格（11 列含情报状态自动匹配）+ 批量导入/导出按钮
  ③ 分隔提示行（显示当前选中股）
  ④ 分析区：DetailStrip + K 线图 + 5 Agent 详情面板（_AgentDetailPanel 平铺 Section）

AI 分析走 v3.0 统一引擎 predict_unified：
  Agent1 市场环境 + Agent3 基本面 + Agent4 技术面 + Agent5 资金流向 + Agent6 事件催化（并行 V4.1 Flash thinking）
  → F 层多空辩论裁决（V4.1 Flash thinking）→ Z 层时间维度 → G 层 V4.1 Flash thinking 决策
  个股 AI 分析链路统一使用 deepseek-flash (V4.1 Flash) thinking，并受资料边界硬约束限制。

支持 kind='stock' 个股分析 + kind='sector' 板块分析（复用同一引擎）。
"""
from __future__ import annotations

import json
import numpy as np
from datetime import date, datetime
from pathlib import Path

from core.qt_runtime import configure_qt_runtime
configure_qt_runtime()

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavToolbar
from matplotlib.figure import Figure

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPropertyAnimation, QRect, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QLabel, QListWidget, QListWidgetItem,
    QDialog, QLineEdit, QTextBrowser, QScrollArea,
    QFrame, QMessageBox, QInputDialog, QTabWidget,
    QDoubleSpinBox, QSpinBox,
    QStackedWidget,
    QFormLayout, QDialogButtonBox, QApplication,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSizePolicy, QCheckBox, QComboBox,
    QGridLayout, QToolTip,
)
from PySide6.QtGui import QFont, QColor

from core.constants import MUTED, RED, GREEN
from core.data_source import fmt_price, quotes_routed
from core.credentials import load_api_key as _load_api_key, save_api_key as _save_api_key
from core.predictor import load_today_predictions
from core.virtual_portfolio import add_virtual_position
from core.portfolio_data import (
    load_holdings, save_holdings, add_holding,
    update_holding, remove_holding, compute_kpis,
)
from core.cache import (
    load_holdings_pnl_calendar, save_holdings_pnl_calendar,
    compute_buy, compute_sell,
    get_default_position_value,
)
from core.holdings_pnl_calendar import (
    build_holding_day_change,
    build_holdings_pnl_snapshot,
    format_holdings_pnl_tooltip,
    frozen_snapshot_matches_sources,
    holdings_pnl_source_key,
    pnl_calendar_update_phase,
)
from core.trade_calendar import is_trade_day
from core.tracking import (
    load_tasks, create_task, pending_maintenance_ids,
)
from core.tracking_display import (
    format_price_or_dash,
    format_prediction_advice_lines,
    format_prediction_copy_field_lines,
    tracking_reference_price,
)

from core.paths import HOME
_CONFIG_FILE = HOME / 'intel_config.json'

_DIR_LABELS = {
    'bullish': ('看多 ↑', '#26a69a'),
    'bearish': ('看跌回避 ↓', '#ef5350'),
    'neutral': ('中性 →', '#888888'),
}

_ZH_REPLACEMENTS = {
    'strong_buy': '强烈看多',
    'strong_sell': '强烈回避',
    'watch_only': '仅观察',
    'not_suitable': '不适合',
    'bullish': '看多',
    'bearish': '看跌回避',
    'neutral': '中性',
    'buy': '买入',
    'sell': '卖出',
    'hold': '观望',
}


def _zh_text(text: str) -> str:
    out = str(text or '')
    for raw, label in _ZH_REPLACEMENTS.items():
        out = out.replace(raw, label)
    return out


def _behavior_tags_of(pred) -> list:
    """从预测结果里取代码生成的技术行为标签（位于 belief_snapshot，兼容顶层/技术画像）。"""
    if not isinstance(pred, dict):
        return []
    snap = pred.get('belief_snapshot') or {}
    tags = snap.get('technical_behavior_tags') if isinstance(snap, dict) else None
    if not tags:
        tags = pred.get('technical_behavior_tags')
    if not tags:
        prof = pred.get('_technical_profile') or pred.get('technical_profile') or {}
        if isinstance(prof, dict):
            tags = prof.get('behavior_tags')
    return tags or []


def _behavior_tags_html(tags) -> str:
    """把代码量化生成的技术行为标签（疑似X + 依据 + 失效条件）渲染为 HTML，附在技术面 Agent 下方。"""
    import html as _html
    if not isinstance(tags, list) or not tags:
        return ''
    tags = sorted(
        [t for t in tags if isinstance(t, dict)],
        key=lambda t: int(t.get('confidence') or 0), reverse=True,
    )
    if not tags:
        return ''
    parts = ['<br><span style="color:#8899aa;">技术行为标签（代码量化生成 · 仅疑似判断）：</span>']
    for t in tags:
        name = _html.escape(str(t.get('tag', '')))
        conf = int(t.get('confidence') or 0)
        ev = '；'.join(_html.escape(str(x)) for x in (t.get('evidence') or []))
        ce = '；'.join(_html.escape(str(x)) for x in (t.get('counter_evidence') or []))
        parts.append(f'&nbsp;&nbsp;<b style="color:#7ec8e3;">{name}</b> '
                     f'<span style="color:#f0b429;">[置信 {conf}/10]</span>')
        if ev:
            parts.append(f'&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#8899aa;">依据：</span>{ev}')
        if ce:
            parts.append(f'&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#8899aa;">失效：</span>{ce}')
    return '<br>'.join(parts)


def _behavior_tags_text(tags) -> str:
    """技术行为标签的纯文本版（用于复制/导出视图）。"""
    if not isinstance(tags, list) or not tags:
        return ''
    tags = sorted(
        [t for t in tags if isinstance(t, dict)],
        key=lambda t: int(t.get('confidence') or 0), reverse=True,
    )
    if not tags:
        return ''
    lines = []
    for t in tags:
        conf = int(t.get('confidence') or 0)
        lines.append(f'• {t.get("tag", "")} [置信 {conf}/10]')
        ev = '；'.join(str(x) for x in (t.get('evidence') or []))
        ce = '；'.join(str(x) for x in (t.get('counter_evidence') or []))
        if ev:
            lines.append(f'    依据：{ev}')
        if ce:
            lines.append(f'    失效：{ce}')
    return '\n'.join(lines)


def _intel_digest_html(digest) -> str:
    """把本次喂给事件催化 Agent 的情报列表渲染为一行摘要，置于该段顶部。"""
    import html as _html
    if not isinstance(digest, list) or not digest:
        return ''
    _DIR_LABEL = {'bullish': '利好', 'bearish': '利空', 'neutral': '中性'}
    _DIR_COLOR = {'bullish': '#e84040', 'bearish': '#00c853', 'neutral': '#888888'}
    rows = []
    for d in digest[:8]:
        title = _html.escape(str(d.get('title', ''))[:26])
        dirk = d.get('direction', 'neutral')
        tag = _DIR_LABEL.get(dirk, '')
        color = _DIR_COLOR.get(dirk, '#888888')
        rows.append(
            f'&nbsp;&nbsp;<span style="color:{color};">[{tag}]</span> {title}'
        )
    more = f'（仅列前 8 条，共 {len(digest)} 条）' if len(digest) > 8 else ''
    head = (
        f'<span style="color:#8899aa;">本次纳入 {len(digest)} 条情报{more}：</span>'
    )
    return '<br>'.join([head, *rows]) + '<br><br>'


_FONT_BASE_PT = 10


def _current_font_pt() -> int:
    app = QApplication.instance()
    pt = app.font().pointSize() if app else _FONT_BASE_PT
    return pt if pt and pt > 0 else _FONT_BASE_PT


def _scaled_pt(pt: int | float) -> float:
    return max(7, round(float(pt) * _current_font_pt() / _FONT_BASE_PT, 1))


def _set_scaled_style(widget: QWidget, css: str) -> None:
    """Apply CSS scaled to the current global font and preserve the unscaled CSS.

    MainWindow later reads `_orig_ss` when the user switches font size. Dynamic
    portfolio sections are often rebuilt after that switch, so they need the
    same origin CSS metadata instead of freezing at the construction size.
    """
    widget.setProperty('_orig_ss', css)
    scale = _current_font_pt() / _FONT_BASE_PT

    def _repl(match):
        val = float(match.group(1))
        unit = match.group(2)
        if unit == 'pt':
            return f'font-size: {max(7, round(val * scale, 1))}pt'
        return f'font-size: {max(8, int(round(val * scale)))}px'

    import re as _re
    widget.setStyleSheet(_re.sub(r'font-size\s*:\s*(\d+(?:\.\d+)?)(px|pt)\b', _repl, css))


_FIELD_SS = (
    'background: #1a1a2e; color: #e0e0e0; border: 1px solid #3a4a6a;'
    ' border-radius: 4px; padding: 4px 8px;'
)
_BTN_RED = (
    'QPushButton { background:#e94560; color:#fff; border:none;'
    ' border-radius:4px; padding:6px 16px; }'
    'QPushButton:hover { background:#c73652; }'
    'QPushButton:disabled { background:#333; color:#666; }'
)
_BTN_DIM = (
    'QPushButton { background:#2a3a5a; color:#aaa; border:none;'
    ' border-radius:4px; padding:6px 16px; }'
    'QPushButton:hover { background:#3a4a6a; color:#fff; }'
)




# =========================================================
# 后台预测线程（从旧 _PredictWorker 原样保留）
# =========================================================
class _PredictWorker(QThread):
    finished = Signal(object, str, str, str)
    status_update = Signal(str)   # 进度文字

    def __init__(self, code, name, api_key,
                 intel_events_snapshot,    # 已在主线稏 snapshot
                 context,                  # 包含 sector/emotion/global 快照
                 sector_context_snapshot,  # 原始 sector_context 字典，用于板块强度计算
                 current_price,
                 force_refresh=False, cognition=None, parent=None):
        super().__init__(parent)
        self._code = code
        self._name = name
        self._api_key = api_key
        self._intel_events_snapshot = intel_events_snapshot
        self._context = context
        self._sector_context_snapshot = sector_context_snapshot
        self._current_price = current_price
        self._force_refresh = force_refresh
        self._cognition = cognition

    def run(self):
        # 并发拉取 K线/财务/资金流 + emotion + global
        intel_checked_at = ''
        intel_check_error = ''
        try:
            self.status_update.emit('正在拉取市场数据…')
            from core.data_orchestrator import gather_stock_context
            stock_ctx = gather_stock_context(self._code, self._name,
                                             force_refresh=self._force_refresh)
            merged = dict(self._context)
            merged['kline_summary'] = stock_ctx.get('kline_summary', '')
            merged['technical_profile'] = stock_ctx.get('technical_profile')
            merged['fundamental_summary'] = stock_ctx.get('fundamental_summary', '')
            merged['money_flow_summary'] = stock_ctx.get('money_flow_summary', '')
            merged['flow_profile'] = stock_ctx.get('flow_profile')
            merged['restricted_release'] = stock_ctx.get('restricted_release')
            merged['hot_rank'] = stock_ctx.get('hot_rank')
            realtime = stock_ctx.get('realtime') or {}
            # 腾讯实时价优先；若腾讯失败则看 kline close，两源偏离 > 35% 时警告
            tencent_price = realtime.get('current_price')
            kline_close = stock_ctx.get('_kline_last_close')
            if tencent_price and tencent_price > 0:
                self._current_price = tencent_price
                self._price_source = realtime.get('_price_source', 'tencent')
            elif kline_close and kline_close > 0:
                self._current_price = kline_close
                self._price_source = 'kline_close'
            # 若 kline 和 tencent 都有，但偏离 >35%，发出警告更新
            if tencent_price and kline_close and tencent_price > 0 and kline_close > 0:
                ratio = abs(kline_close - tencent_price) / tencent_price
                if ratio > 0.35:
                    self.status_update.emit(
                        f'⚠️ 价格异常：K线close={kline_close:.3f} vs 实时{tencent_price:.3f}'
                        f'（偏离{ratio*100:.0f}%），已用实时价'
                    )
                    self._price_source = 'tencent_override'
            # emotion/global: stock_ctx 已并发拉取，优先用；外部 push 次之
            merged['emotion'] = stock_ctx.get('emotion') or self._context.get('emotion')
            merged['global'] = stock_ctx.get('global') or self._context.get('global')
        except Exception:
            merged = dict(self._context)
            self._price_source = getattr(self, '_price_source', 'fallback')

        # 智能情报筛选：名称/代码关键词 × 板块 三维匹配
        self.status_update.emit('筛选相关情报…')
        sectors: list[str] = []
        try:
            from core.intel_matcher import (
                load_intel_feed, filter_relevant_intel,
                infer_sectors_for_stock, sector_strength_text,
            )
            all_events = self._intel_events_snapshot or load_intel_feed()
            sectors = infer_sectors_for_stock(
                self._code, self._name, self._sector_context_snapshot
            )
            filtered_intel = filter_relevant_intel(
                all_events, self._code, self._name, sectors, max_n=5
            )
            merged['fund_context'] = sector_strength_text(sectors, self._sector_context_snapshot)
            intel_checked_at = datetime.now().isoformat(timespec='seconds')
        except Exception as e:
            filtered_intel = self._intel_events_snapshot or []
            intel_checked_at = datetime.now().isoformat(timespec='seconds')
            intel_check_error = str(e)

        # GitHub 开源生态情报（限时 15s，失败静默跳过）
        github_intel_raw: list[dict] = []
        if sectors:
            self.status_update.emit('拉取 GitHub 开源生态情报…')
            try:
                from core.github_intel import fetch_github_intel_for_sectors
                from core.intel_models import IntelEvent
                github_intel_raw = fetch_github_intel_for_sectors(sectors, days=14)
                for raw in github_intel_raw:
                    try:
                        ev = IntelEvent.from_dict(raw)
                        if ev not in filtered_intel:
                            filtered_intel.append(ev)
                    except Exception:
                        pass
            except Exception:
                pass

        # 拉取个股专属新闻（懒加载，只在分析时拉取）
        stock_news: list[dict] = []
        stock_news_checked_at = ''
        stock_news_error = ''
        try:
            self.status_update.emit('拉取个股专属新闻…')
            from core.stock_news_provider import fetch_stock_news
            stock_news = fetch_stock_news(self._code, limit=10, force=self._force_refresh)
            stock_news_checked_at = datetime.now().isoformat(timespec='seconds')
        except Exception as e:
            stock_news_checked_at = datetime.now().isoformat(timespec='seconds')
            stock_news_error = str(e)

        # 加载用户画像
        try:
            from core.user_profile import load_profile
            profile = load_profile()
        except Exception:
            profile = None

        self.status_update.emit('数据就绪，5个Agent + 多空辩论推理中…')
        # 兜底：predict_unified 正常应返回 err，不应抛异常；万一抛出也保证 finished 必 emit，
        # 否则批量队列里该 worker 永留 active 导致卡死。
        try:
            from core.predictor import predict_unified
            pred, reasoning, cache_key, err = predict_unified(
                self._code, self._name, self._api_key,
                filtered_intel, merged, self._current_price,
                kind='stock',
                profile=profile,
                stock_news=stock_news,
                sectors=sectors,
                force_refresh=self._force_refresh,
                cognition=self._cognition,
            )
        except Exception as e:
            pred, reasoning, cache_key, err = None, '', '', f'分析异常: {e}'
        if pred is not None:
            pred['_stock_news'] = stock_news
            pred['_kline_summary'] = merged.get('kline_summary', '')
            pred['_technical_profile'] = merged.get('technical_profile')
            pred['_fundamental_summary'] = merged.get('fundamental_summary', '')
            pred['_money_flow_summary'] = merged.get('money_flow_summary', '')
            pred['_flow_profile'] = merged.get('flow_profile')
            pred['_restricted_release'] = merged.get('restricted_release')
            pred['_hot_rank'] = merged.get('hot_rank')
            pred['_intel_digest'] = [
                {'title': getattr(e, 'title', ''),
                 'direction': getattr(e, 'direction', '')}
                for e in filtered_intel
            ]
            pred['_emotion'] = merged.get('emotion')
            pred['_global'] = merged.get('global')
            pred['_price_source'] = getattr(self, '_price_source', 'unknown')
            pred['_github_intel'] = github_intel_raw
            pred['_sectors'] = sectors
            pred['_stock_news_checked_at'] = stock_news_checked_at
            pred['_intel_checked_at'] = intel_checked_at
            if stock_news_error:
                pred['_stock_news_error'] = stock_news_error
            if intel_check_error:
                pred['_intel_check_error'] = intel_check_error
            try:
                from core.kline_provider import fetch_daily_kline
                df_k = fetch_daily_kline(self._code, days=80)
                if df_k is not None and not df_k.empty:
                    df_k = df_k.copy()
                    df_k.columns = df_k.columns.str.lower()
                    vol_col = next((c for c in ('volume', 'vol') if c in df_k.columns), None)
                    rows = []
                    for idx, row in df_k.iterrows():
                        rows.append({
                            'date': str(idx.date()),
                            'open': float(row.get('open') or 0),
                            'high': float(row.get('high') or 0),
                            'low': float(row.get('low') or 0),
                            'close': float(row.get('close') or 0),
                            'volume': float(row.get(vol_col) or 0) if vol_col else 0,
                        })
                    pred['_kline_data'] = rows
            except Exception:
                pass
        self.finished.emit(pred, reasoning, cache_key, err)


# =========================================================
# K线独立拉取线程
# =========================================================
class _KlineWorker(QThread):
    finished = Signal(list, str)   # kline_rows, error_msg

    def __init__(self, code: str, parent=None, days: int = 120):
        super().__init__(parent)
        self._code = code
        self._days = days

    def run(self):
        try:
            from core.kline_provider import fetch_daily_kline
            df = fetch_daily_kline(self._code, days=self._days)
            if df is None or df.empty:
                self.finished.emit([], '无K线数据')
                return
            df = df.copy()
            df.columns = df.columns.str.lower()
            vol_col = next((c for c in ('volume', 'vol') if c in df.columns), None)
            rows = []
            for idx, row in df.iterrows():
                rows.append({
                    'date': str(idx.date()),
                    'open': float(row.get('open') or 0),
                    'high': float(row.get('high') or 0),
                    'low': float(row.get('low') or 0),
                    'close': float(row.get('close') or 0),
                    'volume': float(row.get(vol_col) or 0) if vol_col else 0,
                })
            self.finished.emit(rows, '')
        except Exception as e:
            self.finished.emit([], str(e))


# =========================================================
# 持仓价格刷新线程
# =========================================================
def _holding_long_option_codes() -> list[str]:
    """收益月历用：持有中的买入权利仓期权（仅 8 位数字代码可拉行情）。"""
    try:
        from core.cache import load_option_watchlist
        recs = load_option_watchlist()
    except Exception:
        return []
    out = []
    for r in recs:
        if r.get('status') != 'holding' or r.get('position_side') != 'long_right':
            continue
        code = str(r.get('code', '')).strip()
        if code.isdigit() and len(code) == 8:
            out.append(code)
    return out


class _PriceRefreshWorker(QThread):
    finished = Signal(dict)   # {code: {'price': float, ...}}

    def __init__(self, codes: list[str], option_codes: list[str] | None = None, parent=None):
        super().__init__(parent)
        self._codes = codes
        self._option_codes = option_codes or []

    def run(self):
        prices: dict = {}
        try:
            from core.data_source import quotes_routed
            prices = quotes_routed(self._codes) if self._codes else {}
        except Exception:
            prices = {}
        # 期权行情（含前结 pre_close）按合约代码并入同一字典，键不与 6 位股票冲突
        if self._option_codes:
            try:
                from core.option_quote_provider import fetch_option_quotes
                for code, q in fetch_option_quotes(self._option_codes).items():
                    if q.get('ok'):
                        prices[code] = q
            except Exception:
                pass
        self.finished.emit(prices)


# =========================================================
# 添加/编辑持仓对话框（8 字段完整版）
# =========================================================
class _HoldingDialog(QDialog):
    """添加/编辑持仓表单。"""

    def __init__(
        self,
        parent=None,
        prefill: dict | None = None,
        current_price: float | None = None,
    ):
        super().__init__(parent)
        self._prefill = prefill or {}
        self._is_edit = bool(prefill)
        self._current_price = current_price
        self.setWindowTitle('编辑持仓' if self._is_edit else '添加持仓')
        self.setStyleSheet('background-color: #0d0d1f; color: #e0e0e0;')
        self._build_form()

    def _build_form(self):
        self.setFixedSize(380 if self._is_edit else 340, 460 if self._is_edit else 380)

        lbl_ss = 'color: #aaa; font-size: 12px;'

        def _lbl(t):
            l = QLabel(t)
            l.setStyleSheet(lbl_ss)
            return l

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(18, 18, 18, 18)

        layout.addWidget(_lbl('股票代码'))
        self._code_edit = QLineEdit(self._prefill.get('code', ''))
        self._code_edit.setPlaceholderText('6位数字')
        self._code_edit.setStyleSheet(_FIELD_SS)
        if self._is_edit:
            self._code_edit.setReadOnly(True)
            self._code_edit.setStyleSheet(_FIELD_SS + ' background-color: #1a1a2e; color: #666;')
        layout.addWidget(self._code_edit)

        layout.addWidget(_lbl('名称（可选）'))
        self._name_edit = QLineEdit(self._prefill.get('name', ''))
        self._name_edit.setPlaceholderText('如 贵州茅台')
        self._name_edit.setStyleSheet(_FIELD_SS)
        layout.addWidget(self._name_edit)

        if self._is_edit:
            self._build_edit_controls(layout, _lbl)
        else:
            self._build_direct_fields(layout, _lbl)

        layout.addStretch(1)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText('保存' if self._is_edit else '添加')
        btns.button(QDialogButtonBox.StandardButton.Ok).setStyleSheet(_BTN_RED)
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        btns.button(QDialogButtonBox.StandardButton.Cancel).setStyleSheet(_BTN_DIM)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _build_direct_fields(self, layout, _lbl):
        _mv = get_default_position_value()
        layout.addWidget(_lbl(f'持有数量（股，留空按{_mv}元市值默认）'))
        self._shares_edit = QLineEdit()
        self._shares_edit.setPlaceholderText(f'留空=ROUND({_mv}/现价,-2)')
        self._shares_edit.setStyleSheet(_FIELD_SS)
        if self._prefill.get('shares'):
            self._shares_edit.setText(str(int(self._prefill['shares'])))
        layout.addWidget(self._shares_edit)

        layout.addWidget(_lbl('加入时价（元）'))
        self._cost_edit = QLineEdit()
        self._cost_edit.setPlaceholderText('留空=加入时现价')
        self._cost_edit.setStyleSheet(_FIELD_SS)
        if self._prefill.get('cost_price'):
            self._cost_edit.setText(str(self._prefill['cost_price']))
        layout.addWidget(self._cost_edit)

    def _build_edit_controls(self, layout, _lbl):
        self._mode_tabs = QTabWidget()
        self._mode_tabs.setMaximumHeight(34)
        self._mode_tabs.addTab(QWidget(), '直接修改')
        self._mode_tabs.addTab(QWidget(), '买入')
        self._mode_tabs.addTab(QWidget(), '卖出')
        self._mode_tabs.currentChanged.connect(self._on_mode_changed)
        layout.addWidget(self._mode_tabs)

        self._form_stack = QStackedWidget()
        direct_w = QWidget()
        direct_lay = QVBoxLayout(direct_w)
        direct_lay.setContentsMargins(0, 0, 0, 0)
        direct_lay.setSpacing(8)
        self._build_direct_fields(direct_lay, _lbl)
        self._form_stack.addWidget(direct_w)

        trade_w = QWidget()
        trade_form = QFormLayout(trade_w)
        trade_form.setContentsMargins(0, 4, 0, 0)
        trade_form.setSpacing(8)

        self._trade_shares = QSpinBox()
        self._trade_shares.setRange(100, 999999999)
        self._trade_shares.setSingleStep(100)
        self._trade_shares.setValue(100)
        self._trade_shares.valueChanged.connect(self._update_preview)
        trade_form.addRow('交易数量:', self._trade_shares)

        self._trade_price = QDoubleSpinBox()
        self._trade_price.setRange(0.001, 999999.999)
        self._trade_price.setDecimals(3)
        default_price = self._current_trade_price()
        self._trade_price.setValue(default_price if default_price > 0 else 0.001)
        self._trade_price.valueChanged.connect(self._update_preview)
        trade_form.addRow('成交价:', self._trade_price)

        self._form_stack.addWidget(trade_w)
        layout.addWidget(self._form_stack)

        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            'background:#1a1a30; padding:8px; font-size:10pt; border-radius:4px;'
        )
        layout.addWidget(self._preview)
        self._mode_tabs.setCurrentIndex(0)
        self._update_preview()

    def _current_trade_price(self) -> float:
        try:
            cur = float(self._current_price or 0)
        except (TypeError, ValueError):
            cur = 0.0
        if cur > 0:
            return cur
        try:
            return float(self._prefill.get('cost_price') or 0)
        except (TypeError, ValueError):
            return 0.0

    def _on_mode_changed(self, idx: int):
        self._form_stack.setCurrentIndex(0 if idx == 0 else 1)
        self._update_preview()

    def _update_preview(self):
        if not self._is_edit or not hasattr(self, '_preview'):
            return
        mode = self._mode_tabs.currentIndex()
        code = self._prefill.get('code', '')
        cur_shares = int(self._prefill.get('shares') or 0)
        cur_cost = float(self._prefill.get('cost_price') or 0)
        self._preview.setStyleSheet(
            'background:#1a1a30; padding:8px; font-size:10pt; border-radius:4px;'
        )

        if mode == 0:
            shares_text = self._shares_edit.text().strip() or '0'
            cost_text = self._cost_edit.text().strip() or '0'
            try:
                shares = int(shares_text)
                cost = float(cost_text)
                self._preview.setText(
                    f'修改后: 数量 {shares}股, 成本 ¥{fmt_price(cost, code)}'
                )
            except (TypeError, ValueError):
                self._preview.setText('修改后: 请填写有效数量和成本价')
            return

        price = self._trade_price.value()
        shares = self._trade_shares.value()
        if mode == 1:
            new_shares, new_cost = compute_buy(cur_shares, cur_cost, shares, price)
            self._preview.setText(
                f'买入后: 数量 {new_shares}股, 加权均价 ¥{fmt_price(new_cost, code)}'
            )
            return

        new_shares = compute_sell(cur_shares, shares)
        if new_shares is None:
            self._preview.setStyleSheet(
                'background:#3a1010; padding:8px; font-size:10pt; border-radius:4px;'
            )
            self._preview.setText(f'错误: 卖出 {shares}股 超过当前持仓 {cur_shares}股')
        else:
            self._preview.setText(
                f'卖出后: 剩余 {new_shares}股, 成本不变 ¥{fmt_price(cur_cost, code)}'
            )

    def _on_ok(self):
        code = self._code_edit.text().strip()
        if not code or len(code) != 6 or not code.isdigit():
            QMessageBox.warning(self, '输入错误', '请输入6位数字股票代码')
            return

        if self._is_edit and hasattr(self, '_mode_tabs') and self._mode_tabs.currentIndex() in (1, 2):
            self._on_trade_ok(code)
            return

        cost_text = self._cost_edit.text().strip()
        shares_text = self._shares_edit.text().strip()
        name = self._name_edit.text().strip()

        quote = {}
        if not cost_text or not shares_text or not name:
            try:
                quote = quotes_routed([code]).get(code, {}) or {}
            except Exception:
                quote = {}
        cur_price = float(quote.get('price') or 0)

        if cost_text:
            try:
                cost = float(cost_text)
                if cost <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                QMessageBox.warning(self, '输入错误', '加入时价格式不正确，请输入正数，或留空')
                return
        else:
            cost = cur_price
        if cost <= 0:
            QMessageBox.warning(self, '无法获取现价', '无法获取现价，请手动填写加入时价')
            return

        if shares_text:
            try:
                shares = int(shares_text)
                if shares <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                QMessageBox.warning(self, '输入错误', '数量格式不正确，请输入正整数，或留空')
                return
        else:
            base = cur_price if cur_price > 0 else cost
            mv = float(get_default_position_value())
            shares = int((mv / base + 50) // 100 * 100) if base > 0 else 0
            if shares <= 0:
                QMessageBox.warning(self, '无法估算数量', '无法按现价估算数量，请手动填写持有数量')
                return

        if not name:
            name = str(quote.get('name') or '').strip()

        self._result_data = {
            'code': code,
            'name': name,
            'cost_price': cost,
            'shares': shares,
            'mode': 'edit' if self._is_edit else 'add',
            'trade_record': None,
        }
        self.accept()

    def _on_trade_ok(self, code: str):
        name = self._name_edit.text().strip() or str(self._prefill.get('name') or '').strip()
        cur_shares = int(self._prefill.get('shares') or 0)
        cur_cost = float(self._prefill.get('cost_price') or 0)
        trade_shares = self._trade_shares.value()
        trade_price = self._trade_price.value()
        mode = self._mode_tabs.currentIndex()

        if mode == 1:
            new_shares, new_cost = compute_buy(cur_shares, cur_cost, trade_shares, trade_price)
            direction = 'buy'
        else:
            new_shares = compute_sell(cur_shares, trade_shares)
            if new_shares is None:
                QMessageBox.warning(self, '输入错误', f'卖出 {trade_shares}股超过当前持仓 {cur_shares}股')
                return
            new_cost = cur_cost
            direction = 'sell'

        self._result_data = {
            'code': code,
            'name': name,
            'cost_price': new_cost,
            'shares': new_shares,
            'mode': direction,
            'trade_record': {
                'direction': direction,
                'shares': trade_shares,
                'price': trade_price,
                'timestamp': datetime.now().isoformat(),
            },
        }
        self.accept()

    def get_data(self) -> dict:
        return dict(self._result_data)


class _CognitionDialog(QDialog):
    def __init__(self, parent=None, holding: dict | None = None, current_price: float | None = None):
        super().__init__(parent)
        self.setWindowTitle('本次持仓认知（可跳过）')
        self.setMinimumWidth(360)
        self.setStyleSheet('QDialog { background:#07101c; color:#d8e0ee; }')
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self._has_position = QCheckBox('当前已持仓')
        self._has_position.setChecked(bool(holding))
        form.addRow('', self._has_position)

        self._cost_spin = QDoubleSpinBox()
        self._cost_spin.setRange(0, 99999.99)
        self._cost_spin.setDecimals(2)
        self._cost_spin.setSingleStep(0.01)
        self._cost_spin.setValue(float((holding or {}).get('cost_price') or 0))
        form.addRow('成本价', self._cost_spin)

        self._shares_spin = QSpinBox()
        self._shares_spin.setRange(0, 999999999)
        self._shares_spin.setValue(int((holding or {}).get('shares') or 0))
        form.addRow('持仓股数', self._shares_spin)
        self._has_position.toggled.connect(self._sync_position_fields)
        lay.addLayout(form)

        tip = QLabel('取消 = 跳过本层建议；填写内容只用于最终决策后的确定性持仓建议，不进入Agent和多空辩论层。')
        tip.setWordWrap(True)
        tip.setStyleSheet('color:#8899aa; font-size:11px;')
        lay.addWidget(tip)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText('确认')
        btns.button(QDialogButtonBox.StandardButton.Cancel).setText('跳过')
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self._sync_position_fields(self._has_position.isChecked())

    def _sync_position_fields(self, checked: bool):
        self._cost_spin.setEnabled(checked)
        self._shares_spin.setEnabled(checked)
        if not checked:
            self._cost_spin.setValue(0)
            self._shares_spin.setValue(0)

    def get_data(self) -> dict:
        has_position = self._has_position.isChecked()
        return {
            'has_position': has_position,
            'direction': 'bullish',
            'cost_price': self._cost_spin.value() if has_position else 0,
            'shares': self._shares_spin.value() if has_position else 0,
            'horizon': '',
        }


# =========================================================
# 生成预测对话框（直接输入代码+名称，或从持仓选）
# =========================================================
class _StockPickerDialog(QDialog):
    def __init__(self, parent=None, prefill_code='', prefill_name=''):
        super().__init__(parent)
        self.setWindowTitle('生成AI预测')
        self.setFixedSize(320, 170)
        self.setStyleSheet('background-color: #0d0d1f; color: #e0e0e0;')

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)

        lay.addWidget(QLabel('股票代码（如 600036）：'))
        self._code_edit = QLineEdit(prefill_code)
        self._code_edit.setPlaceholderText('6位股票代码')
        self._code_edit.setStyleSheet(_FIELD_SS)
        lay.addWidget(self._code_edit)

        lay.addWidget(QLabel('股票名称（如 招商银行）：'))
        self._name_edit = QLineEdit(prefill_name)
        self._name_edit.setPlaceholderText('中文名称')
        self._name_edit.setStyleSheet(_FIELD_SS)
        lay.addWidget(self._name_edit)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton('开始分析')
        ok_btn.setStyleSheet(_BTN_RED)
        ok_btn.clicked.connect(self._on_ok)
        cancel_btn = QPushButton('取消')
        cancel_btn.setStyleSheet(_BTN_DIM)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

        self._code = ''
        self._name = ''

    def _on_ok(self):
        code = self._code_edit.text().strip()
        name = self._name_edit.text().strip()
        if not code or len(code) != 6 or not code.isdigit():
            QMessageBox.warning(self, '输入错误', '请输入6位数字股票代码')
            return
        if not name:
            QMessageBox.warning(self, '输入错误', '请输入股票名称')
            return
        self._code = code
        self._name = name
        self.accept()

    def get_input(self):
        return self._code, self._name


# =========================================================
# 嵌入式 K 线图 Canvas（60日日线 + MA5/20/60 + 成交量）
# =========================================================
class _KlineCanvas(QWidget):
    """K 线图容器：FigureCanvas + 周期按钮 + 滚轮缩放 + 拖拽平移 + 十字光标。

    交互特性：
      - 顶部 30/60/120/250 日周期按钮（period_changed 信号）
      - 滚轮：以鼠标所在 x 为中心缩放
      - 鼠标左键拖拽：平移 x 轴
      - 双击：还原视图
      - 十字光标 + NavToolbar
    """
    _BG = '#080d1a'
    period_changed = Signal(int)   # 新周期天数

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── 周期选择按钮行 ──────────────
        period_row = QHBoxLayout()
        period_row.setContentsMargins(4, 4, 4, 6)
        period_row.setSpacing(2)
        period_row.addStretch()
        self._period_buttons: dict[int, QPushButton] = {}
        self._current_period = 120
        for days in (30, 60, 120, 250):
            btn = QPushButton(f'{days}日')
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.setFixedHeight(26)
            btn.adjustSize()
            btn.setStyleSheet(
                'QPushButton { background:#0a0f1a; color:#7a9ab0; border:1px solid #1a2a3a;'
                ' border-radius:3px; font-size:10pt; padding: 2px 6px; }'
                'QPushButton:hover { color:#e0e0e0; border-color:#3a5a7a; }'
                'QPushButton:checked { background:#1a2a40; color:#e94560;'
                ' border:1px solid #e94560; font-weight:bold; }'
            )
            btn.clicked.connect(lambda _checked, d=days: self._on_period_click(d))
            self._period_buttons[days] = btn
            period_row.addWidget(btn)
        self._period_buttons[120].setChecked(True)
        period_row.addStretch()
        period_wrap = QWidget()
        period_wrap.setLayout(period_row)
        period_wrap.setMaximumHeight(42)
        lay.addWidget(period_wrap)

        fig = Figure(figsize=(6, 2.6), dpi=80)
        fig.patch.set_facecolor(self._BG)
        self._fig = fig
        self._canvas = FigureCanvas(fig)
        self._canvas.setMinimumHeight(165)
        self._canvas.setMaximumHeight(195)

        # NavigationToolbar（只保留 pan / zoom / home，隐藏其余按钮）
        self._toolbar = NavToolbar(self._canvas, self)
        self._toolbar.setStyleSheet(
            'QToolBar { background:#0a0f1a; border:none; spacing:2px; }'
            'QToolButton { color:#7a9ab0; background:transparent; border:none;'
            ' border-radius:3px; padding:2px; font-size:10px; }'
            'QToolButton:hover { background:#1a2a3a; }'
        )
        self._toolbar.setMaximumHeight(24)
        # 仅保留 Home / Pan / Zoom（隐藏 Save / Subplots 等）
        _KEEP = {'Home', 'Pan', 'Zoom'}
        for action in self._toolbar.actions():
            if action.text() and action.text() not in _KEEP:
                action.setVisible(False)

        lay.addWidget(self._toolbar)
        lay.addWidget(self._canvas)

        self._ax_k = fig.add_axes([0.01, 0.30, 0.97, 0.68])
        self._ax_v = fig.add_axes([0.01, 0.02, 0.97, 0.26])
        self._style_axes()

        # 十字光标相关
        self._kline_data: list = []
        self._code: str = ''
        self._vline_k = None
        self._vline_v = None
        self._info_text = None
        # 拖拽状态
        self._drag_start_x: float | None = None
        self._drag_start_xlim: tuple | None = None
        self._canvas.mpl_connect('motion_notify_event', self._on_hover)
        self._canvas.mpl_connect('axes_leave_event', self._on_leave)
        # 滚轮 + 拖拽
        self._canvas.mpl_connect('scroll_event', self._on_scroll)
        self._canvas.mpl_connect('button_press_event', self._on_press)
        self._canvas.mpl_connect('button_release_event', self._on_release)

    # ---------- 周期选择 ----------
    def _on_period_click(self, days: int):
        # 切换选中态
        for d, btn in self._period_buttons.items():
            btn.setChecked(d == days)
        self._current_period = days
        self.period_changed.emit(days)

    def current_period(self) -> int:
        return self._current_period

    # ---------- 滚轮缩放 ----------
    def _on_scroll(self, event):
        if event.inaxes not in (self._ax_k, self._ax_v):
            return
        n = len(self._kline_data)
        if n < 2:
            return
        lo, hi = self._ax_k.get_xlim()
        width = hi - lo
        if width <= 0:
            return
        # 缩放因子：向上滚 → 放大（缩小窗口）；向下滚 → 缩小（扩大窗口）
        factor = 0.85 if event.button == 'up' else (1.0 / 0.85)
        new_width = max(5.0, min(float(n), width * factor))
        # 以鼠标位置为锚点
        if event.xdata is not None:
            anchor = event.xdata
            t = (anchor - lo) / width if width > 0 else 0.5
        else:
            t = 0.5
        new_lo = max(-0.5, lo + (1 - factor) * width * t)
        new_hi = min(float(n) - 0.5, new_lo + new_width)
        # 边界修正
        if new_hi - new_lo < 5:
            return
        if new_lo < -0.5:
            new_lo = -0.5
            new_hi = new_lo + new_width
        if new_hi > float(n) - 0.5:
            new_hi = float(n) - 0.5
            new_lo = max(-0.5, new_hi - new_width)
        self._ax_k.set_xlim(new_lo, new_hi)
        self._ax_v.set_xlim(new_lo, new_hi)
        self._canvas.draw_idle()

    # ---------- 拖拽平移 ----------
    def _on_press(self, event):
        # 左键且在 K 线或量能轴内 + 未进入 NavToolbar 模式才启用拖拽
        if event.button != 1:
            return
        if event.inaxes not in (self._ax_k, self._ax_v):
            return
        # NavToolbar 处于 pan/zoom 模式时让它处理
        mode = getattr(self._toolbar, 'mode', '')
        if str(mode).strip():
            return
        self._drag_start_x = event.xdata
        self._drag_start_xlim = self._ax_k.get_xlim()

    def _on_release(self, event):
        self._drag_start_x = None
        self._drag_start_xlim = None

    def _on_drag(self, event) -> bool:
        """处理拖拽事件，返回 True 表示已消费，跳过 hover。"""
        if self._drag_start_x is None or self._drag_start_xlim is None:
            return False
        if event.xdata is None:
            return False
        n = len(self._kline_data)
        if n < 2:
            return False
        dx = event.xdata - self._drag_start_x
        # 注意：dragging 让窗口反方向移动（鼠标向左拖 → 视图向右移）
        lo0, hi0 = self._drag_start_xlim
        new_lo = lo0 - dx
        new_hi = hi0 - dx
        width = hi0 - lo0
        if new_lo < -0.5:
            new_lo = -0.5
            new_hi = new_lo + width
        if new_hi > float(n) - 0.5:
            new_hi = float(n) - 0.5
            new_lo = new_hi - width
        self._ax_k.set_xlim(new_lo, new_hi)
        self._ax_v.set_xlim(new_lo, new_hi)
        self._canvas.draw_idle()
        return True

    def _style_axes(self):
        for ax in (self._ax_k, self._ax_v):
            ax.set_facecolor(self._BG)
            ax.tick_params(axis='both', colors='#556677', labelsize=6)
            for sp in ax.spines.values():
                sp.set_color('#1a2a3a')

    def _on_hover(self, event):
        # 拖拽优先消费 motion 事件
        if self._on_drag(event):
            return
        if event.inaxes not in (self._ax_k, self._ax_v) or not self._kline_data:
            return
        xi = int(round(event.xdata)) if event.xdata is not None else -1
        n = len(self._kline_data)
        if not (0 <= xi < n):
            return
        d = self._kline_data[xi]
        # 更新/创建竖线
        x = xi
        for attr, ax in (('_vline_k', self._ax_k), ('_vline_v', self._ax_v)):
            old = getattr(self, attr)
            if old:
                old.remove()
            setattr(self, attr, ax.axvline(x=x, color='#4080a0', lw=0.6, alpha=0.7, zorder=10))
        # 右上角文本标注
        if self._info_text:
            self._info_text.remove()
        color = '#26a69a' if d['close'] >= d['open'] else '#ef5350'
        self._info_text = self._ax_k.text(
            0.02, 0.97,
            f"{d['date']}  O:{fmt_price(d['open'], self._code)} H:{fmt_price(d['high'], self._code)}"
            f" L:{fmt_price(d['low'], self._code)} C:{fmt_price(d['close'], self._code)}",
            transform=self._ax_k.transAxes,
            fontsize=7, color=color, va='top', ha='left',
            bbox=dict(facecolor='#0a0f1a', alpha=0.75, edgecolor='none', pad=1.5),
            zorder=11,
        )
        self._canvas.draw_idle()

    def _on_leave(self, event):
        changed = False
        for attr in ('_vline_k', '_vline_v', '_info_text'):
            obj = getattr(self, attr)
            if obj:
                obj.remove()
                setattr(self, attr, None)
                changed = True
        if changed:
            self._canvas.draw_idle()

    def render(self, kline_data: list, code: str = ''):
        self._kline_data = kline_data
        self._code = code
        self._ax_k.cla()
        self._ax_v.cla()
        self._vline_k = self._vline_v = self._info_text = None
        self._style_axes()

        if not kline_data:
            self._canvas.draw_idle()
            return

        n = len(kline_data)
        x = np.arange(n)
        op = np.array([d['open'] for d in kline_data], dtype=float)
        hi = np.array([d['high'] for d in kline_data], dtype=float)
        lo = np.array([d['low'] for d in kline_data], dtype=float)
        cl = np.array([d['close'] for d in kline_data], dtype=float)
        vo = np.array([d['volume'] for d in kline_data], dtype=float)

        w = 0.55
        up_mask = cl >= op
        dn_mask = ~up_mask

        self._ax_k.vlines(x[up_mask], lo[up_mask], hi[up_mask], color='#26a69a', lw=0.7)
        self._ax_k.bar(x[up_mask], cl[up_mask] - op[up_mask], bottom=op[up_mask],
                       width=w, color='#26a69a', linewidth=0, zorder=3)
        self._ax_k.vlines(x[dn_mask], lo[dn_mask], hi[dn_mask], color='#ef5350', lw=0.7)
        self._ax_k.bar(x[dn_mask], op[dn_mask] - cl[dn_mask], bottom=cl[dn_mask],
                       width=w, color='#ef5350', linewidth=0, zorder=3)

        if n >= 5:
            ma5 = np.convolve(cl, np.ones(5) / 5, mode='valid')
            self._ax_k.plot(x[4:], ma5, color='#7ac8ff', lw=0.9, label='MA5')
        if n >= 20:
            ma20 = np.convolve(cl, np.ones(20) / 20, mode='valid')
            self._ax_k.plot(x[19:], ma20, color='#f0a030', lw=0.9, label='MA20')
        if n >= 60:
            ma60 = np.convolve(cl, np.ones(60) / 60, mode='valid')
            self._ax_k.plot(x[59:], ma60, color='#c070f0', lw=0.8, label='MA60')

        vc = np.where(up_mask, '#26a69a', '#ef5350')
        self._ax_v.bar(x, vo, color=vc, width=w, linewidth=0)

        step = max(1, n // 8)
        tpos = x[::step]
        tlbl = [kline_data[i]['date'] for i in range(0, n, step)]
        self._ax_k.set_xticks([])
        self._ax_k.set_xlim(-0.5, n - 0.5)
        self._ax_v.set_xticks(tpos)
        self._ax_v.set_xticklabels(tlbl, fontsize=9, color='#778899', rotation=15, ha='right')
        self._ax_v.set_xlim(-0.5, n - 0.5)
        self._ax_v.yaxis.set_visible(False)

        self._ax_k.yaxis.tick_right()
        self._ax_k.tick_params(axis='y', colors='#7a8a9a', labelsize=7)
        self._ax_k.legend(
            loc='upper left', fontsize=6,
            facecolor='#0d1a2a', edgecolor='#1a2a3a', labelcolor='#888',
            framealpha=0.85, borderpad=0.3, handlelength=1.2,
        )

        self._fig.tight_layout(pad=0.1)
        self._canvas.draw_idle()



# =========================================================
# 情报状态工具函数
# =========================================================
_INTEL_STATUS_MAP = {
    'critical':  ('🔥', '#e84444'),
    'important': ('⚠️', '#f0a030'),
    'info':      ('·',  '#7a9ab0'),
}

def _get_intel_status(events: list, code: str, name: str) -> tuple[str, str]:
    """返回该股最近 7 天内最高 level 的情报 (emoji, color)。"""
    try:
        from datetime import datetime, timedelta
        from core.intel_matcher import filter_relevant_intel
        relevant = filter_relevant_intel(events, code, name, max_n=10)
        if not relevant:
            return '—', '#666677'
        cutoff = datetime.now() - timedelta(days=7)
        best = None
        order = {'critical': 0, 'important': 1, 'info': 2}
        for ev in relevant:
            ts = getattr(ev, 'timestamp', '') or ''
            try:
                t = datetime.fromisoformat(ts[:19])
                if t < cutoff:
                    continue
            except Exception:
                pass
            lvl = getattr(ev, 'level', 'info')
            if best is None or order.get(lvl, 2) < order.get(best, 2):
                best = lvl
        if best:
            emoji, color = _INTEL_STATUS_MAP.get(best, ('·', '#7a9ab0'))
            return emoji, color
    except Exception:
        pass
    return '—', '#666677'


def _find_cached_pred(code: str) -> dict | None:
    """从今日预测历史里找该股最新一条。"""
    try:
        preds = load_today_predictions()
        for p in preds:
            pred = p.get('prediction') or {}
            if pred.get('code') == code:
                pred = dict(pred)
                pred['_reasoning'] = p.get('reasoning', '')
                return pred
    except Exception:
        pass
    return None


# =========================================================
# _HoldingsPnlCalendarWidget / _CalendarDrawer — 真实持仓收益月历
# =========================================================
class _InstantToolTipLabel(QLabel):
    def enterEvent(self, event):
        super().enterEvent(event)
        tip = self.toolTip()
        if tip:
            QToolTip.showText(self.mapToGlobal(self.rect().center()), tip, self)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class _HoldingsPnlCalendarWidget(QWidget):
    """真实持仓收益月历：顶部汇总 + 月份导航 + 日历格子。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        today = date.today()
        self._current_year = today.year
        self._current_month = today.month
        self._records: dict[str, dict] = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)

        self._summary_label = QLabel('本年盈亏: --  本月盈亏: --  今日盈亏: --  总市值: --  总成本: --  缺失: 0只')
        self._summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._summary_label.setWordWrap(True)
        self._summary_label.setFixedHeight(32)
        self._summary_label.setStyleSheet('font-size:8pt; padding:1px; color:#aabbcc;')
        layout.addWidget(self._summary_label)

        nav_style = (
            'QPushButton { background:#1a2a3a; color:#ccd; border:1px solid #2a3a4a;'
            ' border-radius:3px; font-size:9pt; padding:0 3px; }'
            'QPushButton:hover { background:#2a3a5a; color:#fff; }'
        )
        nav = QHBoxLayout()
        nav.setSpacing(4)
        self._prev_btn = QPushButton('◀')
        self._prev_btn.setFixedSize(24, 20)
        self._prev_btn.setStyleSheet(nav_style)
        self._prev_btn.clicked.connect(self._prev_month)
        combo_style = (
            'QComboBox { background:#1a2a3a; color:#fff; border:1px solid #2a3a4a;'
            ' border-radius:3px; font-size:9pt; padding:0 4px; }'
            'QComboBox:hover { background:#2a3a5a; }'
            'QComboBox QAbstractItemView { background:#1a2a3a; color:#ccd;'
            ' selection-background-color:#2a3a5a; }'
        )
        self._year_combo = QComboBox()
        self._year_combo.setFixedHeight(20)
        self._year_combo.setStyleSheet(combo_style)
        for y in range(self._current_year - 5, self._current_year + 2):
            self._year_combo.addItem(f'{y}年', y)
        self._month_combo = QComboBox()
        self._month_combo.setFixedHeight(20)
        self._month_combo.setStyleSheet(combo_style)
        for m in range(1, 13):
            self._month_combo.addItem(f'{m}月', m)
        self._year_combo.currentIndexChanged.connect(self._on_combo_changed)
        self._month_combo.currentIndexChanged.connect(self._on_combo_changed)
        self._today_btn = QPushButton('本月')
        self._today_btn.setFixedSize(36, 20)
        self._today_btn.setStyleSheet(nav_style)
        self._today_btn.clicked.connect(self._go_today)
        self._next_btn = QPushButton('▶')
        self._next_btn.setFixedSize(24, 20)
        self._next_btn.setStyleSheet(nav_style)
        self._next_btn.clicked.connect(self._next_month)
        nav.addWidget(self._prev_btn)
        nav.addStretch(1)
        nav.addWidget(self._year_combo)
        nav.addWidget(self._month_combo)
        nav.addStretch(1)
        nav.addWidget(self._today_btn)
        nav.addWidget(self._next_btn)
        layout.addLayout(nav)
        self._sync_combos()

        dow = QHBoxLayout()
        dow.setSpacing(2)
        for day in ['一', '二', '三', '四', '五', '六', '日']:
            lbl = QLabel(day)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet('color:#556677; font-size:7pt;')
            dow.addWidget(lbl)
        layout.addLayout(dow)

        self._grid = QGridLayout()
        self._grid.setSpacing(3)
        layout.addLayout(self._grid)
        layout.addStretch(1)
        self._rebuild_grid()

    def _prev_month(self):
        if self._current_month == 1:
            self._current_month = 12
            self._current_year -= 1
        else:
            self._current_month -= 1
        self._refresh()

    def _next_month(self):
        if self._current_month == 12:
            self._current_month = 1
            self._current_year += 1
        else:
            self._current_month += 1
        self._refresh()

    def _go_today(self):
        today = date.today()
        self._current_year = today.year
        self._current_month = today.month
        self._refresh()

    def _on_combo_changed(self):
        self._current_year = self._year_combo.currentData()
        self._current_month = self._month_combo.currentData()
        self._rebuild_grid()
        self._update_summary()

    def _sync_combos(self):
        self._year_combo.blockSignals(True)
        self._month_combo.blockSignals(True)
        yi = self._year_combo.findData(self._current_year)
        if yi < 0:
            self._year_combo.addItem(f'{self._current_year}年', self._current_year)
            yi = self._year_combo.findData(self._current_year)
        self._year_combo.setCurrentIndex(yi)
        self._month_combo.setCurrentIndex(self._current_month - 1)
        self._year_combo.blockSignals(False)
        self._month_combo.blockSignals(False)

    def set_records(self, records: list[dict]):
        self._records = {r['date']: r for r in records}
        self._refresh()

    def _refresh(self):
        self._sync_combos()
        self._rebuild_grid()
        self._update_summary()

    def _rebuild_grid(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        import calendar
        today_str = date.today().strftime('%Y-%m-%d')
        first_weekday = date(self._current_year, self._current_month, 1).weekday()
        days_in_month = calendar.monthrange(self._current_year, self._current_month)[1]

        row, col = 0, first_weekday
        for d in range(1, days_in_month + 1):
            date_str = f'{self._current_year}-{self._current_month:02d}-{d:02d}'
            rec = self._records.get(date_str, {})
            pnl = rec.get('total_pnl', 0) or 0
            count = rec.get('stock_count', 0) or 0
            is_today = date_str == today_str

            cell = _InstantToolTipLabel()
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setFixedHeight(52)
            cell.setMinimumWidth(44)

            if count > 0:
                text = f'{d}\n{pnl:+.0f}\n{count}只'
                color = '#e84444' if pnl > 0 else ('#2ecc71' if pnl < 0 else '#aaaaaa')
                tip = format_holdings_pnl_tooltip({'date': date_str, **rec})
            else:
                text = f'{d}\n—'
                color = '#444455'
                tip = format_holdings_pnl_tooltip({'date': date_str})
            cell.setText(text)
            border = '2px solid #e94560' if is_today else '1px solid #2a2a4a'
            cell.setStyleSheet(
                f'border:{border}; border-radius:3px; padding:1px; color:{color}; font-size:8pt;'
            )
            cell.setToolTip(tip)
            self._grid.addWidget(cell, row, col)
            col += 1
            if col > 6:
                col = 0
                row += 1

    def _update_summary(self):
        year_prefix = f'{self._current_year}-'
        month_prefix = f'{self._current_year}-{self._current_month:02d}'
        year_recs = [r for d, r in self._records.items() if d.startswith(year_prefix)]
        month_recs = [r for d, r in self._records.items() if d.startswith(month_prefix)]
        year_pnl = sum(r.get('total_pnl', 0) or 0 for r in year_recs)
        total_pnl = sum(r.get('total_pnl', 0) or 0 for r in month_recs)
        today_str = date.today().strftime('%Y-%m-%d')
        today_rec = self._records.get(today_str, {})
        today_pnl = today_rec.get('total_pnl', 0) or 0
        missing = today_rec.get('missing_count', 0)
        total_val, total_cst = 0.0, 0.0
        for r in month_recs:
            s = r.get('summary', {})
            total_val += s.get('total_value', 0) or 0
            total_cst += s.get('total_cost', 0) or 0
        sign = lambda v: '+' if v >= 0 else ''
        self._summary_label.setText(
            f'本年: {sign(year_pnl)}{year_pnl:,.0f}  本月: {sign(total_pnl)}{total_pnl:,.0f}  今日: {sign(today_pnl)}{today_pnl:,.0f}\n'
            f'市值: {self._fmt(total_val)}  成本: {self._fmt(total_cst)}  缺失: {missing}只'
        )

    @staticmethod
    def _fmt(v: float) -> str:
        if abs(v) >= 1e8:
            return f'{v/1e8:.1f}亿'
        if abs(v) >= 1e4:
            return f'{v/1e4:.0f}万'
        return f'{v:.0f}'


class _CalendarDrawer(QWidget):
    """从右侧滑入的月历抽屉，覆盖在持仓面板上方。"""

    WIDTH = 460
    closed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._open = False
        self.setFixedWidth(self.WIDTH)
        self.hide()

        # QWidget 子类需开启 WA_StyledBackground，背景色才会被绘制（否则透出底层 K 线）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            'background-color: #0d1525; border-left: 2px solid #1e3050;'
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 标题栏
        hdr = QWidget()
        hdr.setFixedHeight(32)
        hdr.setStyleSheet('background:#0a1020; border-bottom:1px solid #1e3050;')
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(10, 0, 6, 0)
        title = QLabel('📅 收益月历')
        title.setStyleSheet('color:#aabbcc; font-size:12px; font-weight:bold;')
        hdr_lay.addWidget(title, 1)
        close_btn = QPushButton('◀ 收起')
        close_btn.setFixedSize(70, 24)
        close_btn.setStyleSheet(
            'QPushButton{background:#f0c000;color:#1a0a00;border:none;'
            'border-radius:3px;font-size:12px;font-weight:bold;padding:0 6px;}'
            'QPushButton:hover{background:#ffe040;}'
        )
        close_btn.clicked.connect(self.close_drawer)
        hdr_lay.addWidget(close_btn)
        layout.addWidget(hdr)

        self._cal = _HoldingsPnlCalendarWidget(self)
        layout.addWidget(self._cal, 1)

        self._anim = QPropertyAnimation(self, b'geometry')
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _target_rect(self, open_: bool) -> QRect:
        p = self.parent()
        h = p.height()
        x_open = p.width() - self.WIDTH
        x_closed = p.width()
        x = x_open if open_ else x_closed
        return QRect(x, 0, self.WIDTH, h)

    def toggle(self):
        if self._open:
            self.close_drawer()
        else:
            self.open_drawer()

    def open_drawer(self):
        if self._open:
            return
        self._open = True
        self._cal._go_today()
        self.show()
        self.raise_()
        p = self.parent()
        start = QRect(p.width(), 0, self.WIDTH, p.height())
        end = self._target_rect(True)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()

    def close_drawer(self):
        if not self._open:
            return
        self._open = False
        start = self._target_rect(True)
        end = self._target_rect(False)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.finished.connect(self._on_close_done)
        self._anim.start()

    def _on_close_done(self):
        try:
            self._anim.finished.disconnect(self._on_close_done)
        except Exception:
            pass
        if self._open:
            return
        self.hide()
        self.closed.emit()

    def reposition(self):
        """父控件 resize 时调用，保持抽屉贴右。"""
        if self._open:
            self.setGeometry(self._target_rect(True))

    def set_records(self, records: list[dict]):
        self._cal.set_records(records)


# =========================================================
# _CollapsibleSection — 可折叠节（通用组件）
# =========================================================
class _CollapsibleSection(QWidget):
    """QPushButton 标题 + content 区域的可折叠节。零新依赖。"""

    def __init__(self, title: str, expanded: bool = False, parent=None):
        super().__init__(parent)
        self._expanded = expanded
        self._text_browser: QTextBrowser | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._toggle_btn = QPushButton(('▼ ' if expanded else '▶ ') + title)
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(expanded)
        self._toggle_btn.setFixedHeight(30)
        self._toggle_css = (
            'QPushButton { background:#0e1a34; color:#c8d8f8; border:1px solid #1a2a4a;'
            ' border-bottom:none; border-top-left-radius:6px; border-top-right-radius:6px;'
            ' text-align:left; padding:0 12px; font-size:12px; font-weight:bold; }'
            'QPushButton:hover { color:#ffffff; }'
            'QPushButton:checked { color:#c8d8f8; }'
        )
        _set_scaled_style(self._toggle_btn, self._toggle_css)
        self._toggle_btn.clicked.connect(self._toggle)
        outer.addWidget(self._toggle_btn)

        self._content = QFrame()
        self._content.setStyleSheet(
            'background:#0a111f; border:1px solid #1a2a4a; border-top:none;'
            ' border-bottom-left-radius:6px; border-bottom-right-radius:6px;'
        )
        self._c_lay = QVBoxLayout(self._content)
        self._c_lay.setContentsMargins(12, 9, 12, 12)
        self._c_lay.setSpacing(4)
        outer.addWidget(self._content)
        self._content.setVisible(expanded)

    def _toggle(self):
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        title = self._toggle_btn.text()[2:]
        self._toggle_btn.setText(('▼ ' if self._expanded else '▶ ') + title)

    def set_widget(self, w: QWidget):
        self._c_lay.addWidget(w)

    def set_text(self, text: str, min_h: int = 60, max_h: int = 260):
        box = QTextBrowser()
        font = QFont('Microsoft YaHei', _current_font_pt())
        box.setFont(font)
        box.setPlainText(text)
        box.setMinimumHeight(min_h)
        box.setMaximumHeight(max_h)
        box.setStyleSheet(
            'QTextBrowser { background:#0a111f; color:#e0e0e0; border:none; padding:2px; }'
        )
        self._c_lay.addWidget(box)
        self._text_browser = box

    def update_text(self, text: str):
        """更新已有 QTextBrowser 的内容；若未建立则建立。"""
        if self._text_browser is None:
            self.set_text(text)
        else:
            self._text_browser.setPlainText(text)

    def reapply_font(self):
        _set_scaled_style(self._toggle_btn, self._toggle_css)
        if self._text_browser is not None:
            self._text_browser.setFont(QFont('Microsoft YaHei', _current_font_pt()))


# =========================================================
# _FlatSection — 始终展开的标题+内容区，高度自适应内容
# =========================================================
class _FlatSection(QWidget):
    """分析面板专用：标题栏 + 内容 QTextBrowser，无折叠按钮，内容全量显示。"""
    anchor_clicked = Signal(str)  # 点击 intel 锚点时传出 event_id

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._browser: QTextBrowser | None = None
        self._html_content: str | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        title_bar = QWidget()
        title_bar.setFixedHeight(30)
        title_bar.setStyleSheet(
            'background:#0e1a34; border:1px solid #1a2a4a;'
            ' border-top-left-radius:6px; border-top-right-radius:6px; border-bottom:none;'
        )
        t_lay = QHBoxLayout(title_bar)
        t_lay.setContentsMargins(12, 0, 10, 0)
        t_lay.setSpacing(0)
        accent = QFrame()
        accent.setFixedWidth(3)
        accent.setFixedHeight(14)
        accent.setStyleSheet('background:#4a82c8; border-radius:1px;')
        lbl = QLabel(title)
        self._title_label = lbl
        self._title_css = (
            'color:#c8d8f8; font-size:12px; font-weight:bold;'
            ' background:transparent; padding-left:6px;'
        )
        _set_scaled_style(lbl, self._title_css)
        t_lay.addWidget(accent)
        t_lay.addWidget(lbl, stretch=1)
        outer.addWidget(title_bar)

        self._content_frame = QFrame()
        self._content_frame.setStyleSheet(
            'background:#0a111f; border:1px solid #1a2a4a; border-top:none;'
            ' border-bottom-left-radius:6px; border-bottom-right-radius:6px;'
        )
        self._c_lay = QVBoxLayout(self._content_frame)
        self._c_lay.setContentsMargins(14, 9, 14, 12)
        self._c_lay.setSpacing(0)
        outer.addWidget(self._content_frame)

    def _on_anchor(self, url):
        href = url.toString()
        if href.startswith('intel_'):
            self.anchor_clicked.emit(href[6:])

    def _make_browser(self, text: str) -> QTextBrowser:
        box = QTextBrowser()
        font = QFont('Microsoft YaHei', _current_font_pt())
        box.setFont(font)
        box.setPlainText(text)
        box.setOpenLinks(False)
        box.anchorClicked.connect(self._on_anchor)
        box.setFrameShape(QFrame.Shape.NoFrame)
        box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        box.setStyleSheet(
            'QTextBrowser { background:#0a111f; color:#d8e4f0; border:none; padding:0; }'
        )
        return box

    def _fit_height(self):
        if self._browser is None:
            return
        doc_h = self._browser.document().size().height()
        self._browser.setFixedHeight(max(40, int(doc_h) + 10))

    def set_text(self, text: str, **_):
        self._html_content = None
        box = self._make_browser(text)
        self._c_lay.addWidget(box)
        self._browser = box
        self._fit_height()

    def update_text(self, text: str):
        if self._browser is None:
            self.set_text(text)
        else:
            self._html_content = None
            self._browser.setPlainText(text)
            self._fit_height()

    def update_html(self, html_str: str):
        if self._browser is None:
            self.set_text('')
        self._html_content = html_str
        font = QFont('Microsoft YaHei', _current_font_pt())
        self._browser.setFont(font)
        self._browser.setHtml(
            f'<div style="color:#d8e4f0;font-size:{_scaled_pt(10)}pt;'
            f'font-family:Microsoft YaHei;line-height:1.6;">{html_str}</div>'
        )
        self._fit_height()

    def reapply_font(self):
        _set_scaled_style(self._title_label, self._title_css)
        if self._browser is None:
            return
        self._browser.setFont(QFont('Microsoft YaHei', _current_font_pt()))
        if self._html_content is not None:
            self._browser.setHtml(
                f'<div style="color:#d8e4f0;font-size:{_scaled_pt(10)}pt;'
                f'font-family:Microsoft YaHei;line-height:1.6;">{self._html_content}</div>'
            )
        self._fit_height()


# =========================================================
# _DetailStrip — 选中持仓一行 KPI（替换旧 _KpiPanel 独立列）
# =========================================================
class _DetailStrip(QFrame):
    ai_analyze_requested    = Signal(str, str)   # code, name
    force_refresh_requested = Signal(str, str)
    delete_requested        = Signal(str)         # holding_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._holding: dict | None = None
        self._is_virtual = False
        self.setFixedHeight(56)
        self.setStyleSheet('background:#0a1020; border-bottom:1px solid #1a2a3a;')

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 4, 12, 4)
        lay.setSpacing(12)

        self._name_lbl = QLabel('← 从持仓表格中选择股票')
        self._name_lbl.setStyleSheet(
            'color:#e0e0e0; font-size:13px; font-weight:bold; min-width:120px;'
        )
        lay.addWidget(self._name_lbl)

        self._price_lbl = QLabel()
        self._price_lbl.setStyleSheet('font-size:16px; font-weight:bold; min-width:60px;')
        lay.addWidget(self._price_lbl)

        self._pnl_lbl = QLabel()
        self._pnl_lbl.setStyleSheet('font-size:13px; font-weight:bold; min-width:70px;')
        lay.addWidget(self._pnl_lbl)

        self._cost_lbl = QLabel()
        self._cost_lbl.setStyleSheet('color:#666677; font-size:11px; min-width:90px;')
        lay.addWidget(self._cost_lbl)

        self._cache_lbl = QLabel()
        self._cache_lbl.setStyleSheet('color:#444455; font-size:10px;')
        lay.addWidget(self._cache_lbl)

        lay.addStretch()

        # 主操作「分析该股」：低调奢华金 — 金属渐变 + 细金边 + 悬停提亮 + 手型光标
        _ai_qss = (
            'QPushButton { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,'
            ' stop:0 #e8cd82, stop:0.5 #cba23f, stop:1 #a87b18);'
            ' color:#3a2600; font-size:14px; font-weight:bold;'
            ' border:1px solid #f2dd97; border-radius:5px; padding:0 22px; }'
            'QPushButton:hover { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,'
            ' stop:0 #f6e3a0, stop:0.5 #e0b955, stop:1 #c4942a);'
            ' border:1px solid #fff2cc; }'
            'QPushButton:pressed { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,'
            ' stop:0 #c2a24e, stop:1 #8f6a12); }'
            'QPushButton:disabled { background:#222; color:#555; border:none; }'
        )
        _force_qss = (
            'QPushButton { background:#1f4fb8; color:#eaf2ff; font-size:11px;'
            ' border:1px solid #3a6ad0; border-radius:4px; padding:0 8px; }'
            'QPushButton:hover { background:#2f63d8; border-color:#5a8aff; }'
            'QPushButton:disabled { background:#222; color:#555; border:none; }'
        )
        _del_qss = (
            'QPushButton { background:#3a1a1a; color:#e87070; font-size:11px;'
            ' border:none; border-radius:4px; padding:0 8px; }'
            'QPushButton:hover { background:#5a2424; color:#ff9090; }'
            'QPushButton:disabled { background:#222; color:#555; }'
        )
        for attr, label, h, qss in [
            ('_ai_btn',    '分析该股',     34, _ai_qss),
            ('_force_btn', '🔄 强制刷新',  28, _force_qss),
            ('_del_btn',   '🗑 删除',      28, _del_qss),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(h)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            setattr(self, attr, btn)
            btn.setStyleSheet(qss)
            lay.addWidget(btn)

        self._ai_btn.clicked.connect(self._on_ai)
        self._force_btn.clicked.connect(self._on_force)
        self._del_btn.clicked.connect(self._on_del)

        self._set_buttons_visible(False)

    def _set_buttons_visible(self, visible: bool):
        for b in (self._ai_btn, self._force_btn, self._del_btn):
            b.setVisible(visible)

    def show_holding(self, holding: dict, cur_price: float | None,
                     total_val: float = 0.0, is_virtual: bool = False):
        self._holding = holding
        self._is_virtual = is_virtual
        code = holding.get('code', '')
        name = holding.get('name', '')
        self._name_lbl.setText(f'{name}（{code}）')
        self._set_buttons_visible(True)

        if is_virtual:
            entry = holding.get('entry_price', 0) or 0
            cur   = cur_price or holding.get('current_price') or entry
            pnl   = holding.get('current_pct', 0) or 0
            self._price_lbl.setText(fmt_price(cur, code))
            color = GREEN if pnl >= 0 else RED
            self._price_lbl.setStyleSheet(f'color:{color}; font-size:16px; font-weight:bold;')
            self._pnl_lbl.setText(f'{"+" if pnl>=0 else ""}{pnl:.2f}%')
            self._pnl_lbl.setStyleSheet(f'color:{color}; font-size:13px; font-weight:bold;')
            self._cost_lbl.setText(f'入场价: {fmt_price(entry, code)}')
            self._del_btn.setText('❌ 平仓')
        else:
            kpi   = compute_kpis(holding, cur_price)
            cur   = kpi['current_price']
            pnl   = kpi['pnl_pct']
            color = GREEN if pnl >= 0 else RED
            self._price_lbl.setText(fmt_price(cur, code))
            self._price_lbl.setStyleSheet(f'color:{color}; font-size:16px; font-weight:bold;')
            sign  = '+' if pnl >= 0 else ''
            self._pnl_lbl.setText(f'{sign}{pnl:.2f}%')
            self._pnl_lbl.setStyleSheet(f'color:{color}; font-size:13px; font-weight:bold;')
            self._cost_lbl.setText(
                f'成本: {fmt_price(holding.get("cost_price",0), code)}  '
                f'市值: ¥{kpi["market_value"]:,.0f}'
            )
            self._del_btn.setText('🗑 删除')
            if kpi.get('stop_triggered'):
                self._cost_lbl.setText(self._cost_lbl.text() + '  ⚠️止损!')
                self._cost_lbl.setStyleSheet('color:#e84444; font-size:11px;')

        # 数据缓存时间
        try:
            from core.data_orchestrator import _context_is_fresh, _load_context, minutes_since_cached
            if _context_is_fresh(code):
                ctx = _load_context(code)
                mins = minutes_since_cached(ctx) if ctx else None
                self._cache_lbl.setText(f'快照: {mins}分钟前' if mins is not None else '快照: 有效')
            else:
                self._cache_lbl.setText('快照: 待拉取')
        except Exception:
            self._cache_lbl.setText('')

    def clear(self):
        self._holding = None
        self._name_lbl.setText('← 从持仓表格中选择股票')
        self._price_lbl.setText('')
        self._pnl_lbl.setText('')
        self._cost_lbl.setText('')
        self._cache_lbl.setText('')
        self._set_buttons_visible(False)

    # ── 按钮回调 ──────────────────────────────────
    def _on_ai(self):
        if self._holding:
            self.ai_analyze_requested.emit(
                self._holding.get('code',''), self._holding.get('name',''))

    def _on_force(self):
        if self._holding:
            self.force_refresh_requested.emit(
                self._holding.get('code',''), self._holding.get('name',''))

    def _on_del(self):
        if not self._holding:
            return
        hid  = self._holding.get('id', '')
        name = self._holding.get('name', self._holding.get('code',''))
        if self._is_virtual:
            reply = QMessageBox.question(
                self, '确认平仓', f'将追踪任务 {name} 标记为已关闭（不计胜负）？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        else:
            reply = QMessageBox.question(
                self, '确认删除', f'确认删除 {name} 的真实持仓？（不可撤销）',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
        if reply == QMessageBox.StandardButton.Yes:
            self.delete_requested.emit(hid)

    def set_ai_enabled(self, enabled: bool):
        self._ai_btn.setEnabled(enabled)
        self._force_btn.setEnabled(enabled)


# =========================================================
# _HoldingsTable — 持仓表格（真实持仓 Schema）
# =========================================================
_REAL_COLS   = ['代码', '名称', '持股数量', '成本均价', '现价', '昨收价', '当日涨跌额', '涨跌幅', '情报状态', '操作']
_VIRTUAL_COLS= ['代码', '名称', '方向', '入场价', '现价', '涨跌幅', '置信度', '到期日']

_DIR_LABELS_VIRT = {
    'bullish': ('看多 ↑', '#26a69a'),
    'bearish': ('看跌回避 ↓', '#ef5350'),
    'neutral': ('中性 →', '#888888'),
}


class _HoldingsTable(QTableWidget):
    row_selected = Signal(dict, bool)   # data, is_virtual
    edit_requested = Signal(dict)       # holding data for editing

    def __init__(self, parent=None):
        super().__init__(parent)
        self._is_virtual = False
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setStyleSheet(
            'QTableWidget { background:#080d1a; border:none; outline:none;'
            ' color:#e0e0e0; font-size:12px; }'
            'QTableWidget::item { padding:3px 6px; border-bottom:1px solid #111820; }'
            'QTableWidget::item:selected { background:#111a30; color:#fff; }'
            'QTableWidget::item:hover:!selected { background:#0e1525; }'
            'QHeaderView::section { background:#0a1020; color:#8899aa;'
            ' border:none; border-bottom:1px solid #1a2a3a;'
            ' padding:3px 6px; font-size:11px; }'
        )
        self.horizontalHeader().setStretchLastSection(False)
        self.verticalHeader().setDefaultSectionSize(34)
        self.verticalHeader().setMinimumSectionSize(34)
        self.currentCellChanged.connect(
            lambda r, _c, _pr, _pc: self._on_row_changed(r)
        )
        self.itemChanged.connect(self._on_cost_edited)

    def load_real(self, holdings: list[dict],
                  cur_prices: dict, intel_events: list):
        self._is_virtual = False
        self.blockSignals(True)
        self.clear()
        self.setColumnCount(len(_REAL_COLS))
        self.setHorizontalHeaderLabels(_REAL_COLS)
        self.setRowCount(len(holdings))

        hh = self.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (0, 2, 3, 4, 5, 6, 7, 8, 9):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        for c, w in enumerate([80, 120, 90, 80, 80, 80, 92, 70, 60, 66]):
            self.setColumnWidth(c, w)

        for r, h in enumerate(holdings):
            code   = h.get('code', '')
            name   = h.get('name', '')
            shares = h.get('shares', 0) or 0
            cost   = h.get('cost_price', 0) or 0
            cur_q  = cur_prices.get(code, {})
            day_metrics = build_holding_day_change(h, cur_q)
            cur    = float(day_metrics['current_price'] or cost)
            pre_close = day_metrics['pre_close']
            day_change = day_metrics['day_change']
            day_pnl = day_metrics['day_pnl']
            pnl    = ((cur - cost) / cost * 100) if cost else 0
            pnl_color = QColor(GREEN if pnl >= 0 else RED)
            sign  = '+' if pnl >= 0 else ''
            day_color = QColor(GREEN if day_change >= 0 else RED) if day_change is not None else None
            pre_text = fmt_price(pre_close, code) if pre_close is not None else '--'
            day_pnl_text = f'{day_pnl:+,.2f}' if day_pnl is not None else '--'
            shares_text = f'{int(shares):,}' if shares else '--'
            intel_emoji, intel_color = _get_intel_status(intel_events, code, name)

            vals: list[tuple[str, QColor | None]] = [
                (code,                  None),
                (name,                  None),
                (shares_text,           None),
                (fmt_price(cost, code), None),
                (fmt_price(cur, code),  None),
                (pre_text,              None),
                (day_pnl_text,          day_color),
                (f'{sign}{pnl:.2f}%',   pnl_color),
                (intel_emoji,           QColor(intel_color)),
            ]
            for c, (text, color) in enumerate(vals):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if color:
                    item.setForeground(color)
                if c != 3:  # 仅成本均价列(3)可双击编辑
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.setItem(r, c, item)
            self.item(r, 0).setData(Qt.ItemDataRole.UserRole, h)

            self.setRowHeight(r, 34)

            btn = QPushButton('编辑')
            btn.setFixedSize(52, 22)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                'QPushButton { background:#243a55; color:#d2e2f4; border:1px solid #3a5e84;'
                ' border-radius:3px; font-size:11px; padding:0; }'
                'QPushButton:hover { background:#345f8c; color:#ffffff; border-color:#5a8ac0; }'
            )
            btn.clicked.connect(lambda checked, h_rec=h: self.edit_requested.emit(h_rec))
            cell = QWidget()
            cell_lay = QHBoxLayout(cell)
            cell_lay.setContentsMargins(0, 0, 0, 0)
            cell_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_lay.addWidget(btn)
            self.setCellWidget(r, 9, cell)

        self.blockSignals(False)

    def load_virtual(self, positions: list[dict], cur_prices: dict):
        self._is_virtual = True
        open_pos = [p for p in positions if p.get('status') == 'open']
        self.blockSignals(True)
        self.clear()
        self.setColumnCount(len(_VIRTUAL_COLS))
        self.setHorizontalHeaderLabels(_VIRTUAL_COLS)
        self.setRowCount(len(open_pos))

        hh = self.horizontalHeader()
        for c in range(len(_VIRTUAL_COLS) - 1):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(len(_VIRTUAL_COLS) - 1, QHeaderView.ResizeMode.Stretch)

        for r, p in enumerate(open_pos):
            code    = p.get('code', '')
            name    = p.get('name', '')
            direction = p.get('direction', 'neutral')
            entry   = p.get('entry_price', 0) or 0
            cur_q   = cur_prices.get(code, {})
            cur     = float(cur_q.get('price') or p.get('current_price') or entry)
            pnl     = ((cur - entry) / entry * 100) if entry else p.get('current_pct', 0) or 0
            pnl_color = QColor(GREEN if pnl >= 0 else RED)
            sign    = '+' if pnl >= 0 else ''
            confidence = p.get('confidence', 0)
            deadline = (p.get('deadline') or '')[:10]
            dir_text, dir_color = _DIR_LABELS_VIRT.get(direction, ('→', '#888888'))

            vals: list[tuple[str, QColor | None]] = [
                (code,                 None),
                (name,                 None),
                (dir_text,             QColor(dir_color)),
                (fmt_price(entry, code), None),
                (fmt_price(cur, code),  None),
                (f'{sign}{pnl:.2f}%',  pnl_color),
                (f'{confidence}/10',   None),
                (deadline,             None),
            ]
            for c, (text, color) in enumerate(vals):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if color:
                    item.setForeground(color)
                self.setItem(r, c, item)
            self.item(r, 0).setData(Qt.ItemDataRole.UserRole, p)

        self.blockSignals(False)

    def _on_cost_edited(self, item: QTableWidgetItem):
        if item.column() != 3:
            return
        id_item = self.item(item.row(), 0)
        if not id_item:
            return
        data = id_item.data(Qt.ItemDataRole.UserRole)
        if not data or not data.get('id'):
            return
        try:
            new_cost = float(item.text())
            if new_cost > 0:
                update_holding(data['id'], cost_price=new_cost)
        except ValueError:
            pass

    def _on_row_changed(self, row: int):
        if row < 0:
            return
        item = self.item(row, 0)
        if not item:
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.row_selected.emit(data, self._is_virtual)


# =========================================================
# _AgentDetailPanel — 右下区（替换旧 _AnalysisView）
# =========================================================
class _AgentDetailPanel(QWidget):
    # 信号保留向后兼容，但已不再使用（追踪任务自动创建）
    virtual_buy_requested = Signal(dict)  # 已废弃
    intel_anchor_clicked = Signal(str)  # event_id

    _SECTION_CONFIG = [
        # (attr,           title,              expanded)
        ('_sec_macro',    '📡 市场环境Agent', True),

        ('_sec_company',  '🏢 基本面Agent',    True),
        ('_sec_tech',     '📈 技术面Agent',    True),
        ('_sec_fundflow', '💰 资金流向Agent',  True),
        ('_sec_news',     '📰 事件催化Agent', True),
        ('_sec_debate',   '⚔️ F层 多空辩论',   True),
        ('_sec_decision', '🎯 G层 最终决策',    True),
        ('_sec_advice',   '📌 操作建议',        True),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 详情条：固定在顶部，不随内容滚动（选中个股 + 分析/刷新/删除常驻可见）
        self._strip = _DetailStrip()
        outer.addWidget(self._strip)

        # 滚动区（详情条之下，仅内容滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet('QScrollArea { background:#080d1a; border:none; }')
        outer.addWidget(scroll, stretch=1)

        container = QWidget()
        container.setStyleSheet('background:#080d1a;')
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(10, 8, 10, 10)
        c_lay.setSpacing(10)
        scroll.setWidget(container)

        # K 线图区（选中持仓后显示）
        self._sec_kline = _CollapsibleSection('📊 K线图', expanded=True)
        self._kline_canvas = _KlineCanvas()
        self._kline_canvas.setFixedHeight(320)
        self._sec_kline.set_widget(self._kline_canvas)
        self._sec_kline.setVisible(False)
        c_lay.addWidget(self._sec_kline)

        # 空状态提示
        self._empty_lbl = QLabel(
            '← 从持仓表格中选择股票，\n'
            '   然后点「分析该股」生成 AI 预测'
        )
        self._empty_css = (
            'color:#444455; font-size:13px; padding:24px 16px;'
        )
        _set_scaled_style(self._empty_lbl, self._empty_css)
        c_lay.addWidget(self._empty_lbl)

        # 平铺文本 sections（始终展开）
        self._text_sections: dict[str, _FlatSection] = {}
        for attr, title, _ in self._SECTION_CONFIG:
            sec = _FlatSection(title)
            sec.set_text('（暂无内容）')
            sec.anchor_clicked.connect(self.intel_anchor_clicked)
            setattr(self, attr, sec)
            self._text_sections[attr] = sec
            c_lay.addWidget(sec)

        # 按钮行
        btn_frame = QFrame()
        btn_frame.setStyleSheet('background:#0a1020; border-top:1px solid #1a2a3a;')
        btn_lay = QHBoxLayout(btn_frame)
        btn_lay.setContentsMargins(8, 6, 8, 6)
        btn_lay.setSpacing(8)

        self._buy_btn = QPushButton('📌 建立追踪')
        self._buy_btn.setFixedHeight(30)
        self._buy_btn.setStyleSheet(
            'QPushButton { background:#1a6a3a; color:#7ef08e; border:none;'
            ' border-radius:4px; font-size:13px; padding:0 12px; }'
            'QPushButton:hover { background:#1f8a4a; }'
            'QPushButton:disabled { background:#1a2a2a; color:#445544; }'
        )
        self._buy_btn.clicked.connect(self._on_buy)
        btn_lay.addWidget(self._buy_btn)

        self._skip_btn = QPushButton('跳过')
        self._skip_btn.setFixedHeight(30)
        self._skip_btn.setStyleSheet(
            'QPushButton { background:#2a3a5a; color:#aaa; border:none;'
            ' border-radius:4px; font-size:13px; padding:0 12px; }'
            'QPushButton:hover { background:#3a4a6a; }'
        )
        self._skip_btn.clicked.connect(self.clear_analysis)
        btn_lay.addWidget(self._skip_btn)

        btn_lay.addStretch()

        self._copy_btn = QPushButton('复制全文')
        self._copy_btn.setFixedHeight(30)
        self._copy_btn.setStyleSheet(
            'QPushButton { background:#2a3a5a; color:#aaa; border:none;'
            ' border-radius:4px; font-size:12px; padding:0 10px; }'
            'QPushButton:hover { background:#3a4a6a; }'
        )
        self._copy_btn.clicked.connect(self._on_copy)
        btn_lay.addWidget(self._copy_btn)

        c_lay.addWidget(btn_frame)
        c_lay.addStretch()

        self._pred: dict | None = None
        self._reasoning: str = ''
        self._hide_analysis_buttons()

    def _hide_analysis_buttons(self):
        for btn in (self._buy_btn, self._skip_btn, self._copy_btn):
            btn.setVisible(False)

    def reapply_font(self):
        """Refresh dynamic analysis text after the global font-size switch."""
        _set_scaled_style(self._empty_lbl, self._empty_css)
        if hasattr(self._sec_kline, 'reapply_font'):
            self._sec_kline.reapply_font()
        for sec in self._text_sections.values():
            sec.reapply_font()
        self._kline_canvas.draw_idle()

    # ── 数据更新 API ─────────────────────────────────────────
    @property
    def strip(self) -> _DetailStrip:
        return self._strip

    @property
    def kline_canvas(self) -> _KlineCanvas:
        return self._kline_canvas

    def show_holding(self, holding: dict, cur_price: float | None,
                     is_virtual: bool = False, total_val: float = 0.0):
        self._strip.show_holding(holding, cur_price, total_val, is_virtual)
        self._sec_kline.setVisible(True)
        self._empty_lbl.setVisible(False)

    @staticmethod
    def _fmt_agent(d) -> str:
        """将 agent 结果（dict 或旧版 str/JSON字符串）格式化为可读文本。"""
        import json
        from core.agents.base import agent_dict_to_text, parse_agent_json
        # 旧版缓存里可能存的是 JSON 字符串
        if isinstance(d, str) and d.strip().startswith('{'):
            try:
                parsed = json.loads(d)
                if isinstance(parsed, dict) and 'stance' in parsed:
                    d = parsed
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(d, dict):
            return agent_dict_to_text(d)
        return str(d) if d else ''

    def show_prediction(self, pred: dict, reasoning: str = '',
                        already_bought: bool = False):
        self._pred     = pred
        self._reasoning = reasoning
        self._empty_lbl.setVisible(False)

        # ── 各 Agent 分析区 ──────────────────────────────────
        agent_mac  = pred.get('_agent_macro')  or ''

        agent_cmp  = pred.get('_agent_company') or ''
        agent_tec  = pred.get('_agent_technical') or pred.get('_agent_tech') or ''
        agent_fund = pred.get('_agent_fundflow') or ''
        agent_news = pred.get('_agent_news') or ''
        debate     = pred.get('_debate', '')   or ''

        # G 层决策：thesis 优先，fallback reasoning
        thesis = _zh_text(pred.get('thesis') or pred.get('reasoning') or '')
        thesis_preview = thesis.strip()
        thesis_full = thesis  # 操作建议区展示全文，不截断
        final_rating = pred.get('final_rating', 'hold') or 'hold'
        _RATING_LABEL = {
            'strong_buy': '强烈买入 ★★', 'buy': '买入 ★',
            'hold': '观望 —',
            'sell': '卖出 ▼', 'strong_sell': '强烈卖出 ▼▼',
        }
        rating_text = _RATING_LABEL.get(final_rating, final_rating)
        g_text = f'综合评级：{rating_text}\n\n{thesis_preview}' if thesis_preview else f'综合评级：{rating_text}'

        # 操作建议：horizon_days → 短/中/长
        horizon = pred.get('horizon_days', 0) or 0
        if not horizon:
            period_label = '—'
        elif horizon <= 5:
            period_label = f'短线（{horizon} 日）'
        elif horizon <= 20:
            period_label = f'中线（{horizon} 日）'
        else:
            period_label = f'长线（{horizon} 日）'

        final_action = pred.get('final_action', 'hold') or 'hold'
        suitability  = pred.get('user_suitability', 'medium') or 'medium'
        entry        = tracking_reference_price(pred)
        invalidation = _zh_text(pred.get('invalidation', '') or '')

        entry_str  = format_price_or_dash(entry, pred.get('code'))
        _SUIT  = {'high': '高▲', 'medium': '中→', 'low': '低▼'}
        _ACT   = {'buy': '买入', 'sell': '卖出', 'hold': '持有/观望',
                  'watch_only': '仅观望', 'not_suitable': '不适合'}
        suit_text = _SUIT.get(suitability, suitability)
        act_text  = _ACT.get(final_action, final_action)
        advice_text = '\n'.join(format_prediction_advice_lines(
            pred,
            period_label=period_label,
            rating_text=rating_text,
            entry_text=entry_str,
            suit_text=suit_text,
            action_text=act_text,
        )) + '\n'
        dist_tags = [
            t for t in _behavior_tags_of(pred)
            if isinstance(t, dict) and t.get('tag') == '出货相'
        ]
        if dist_tags:
            t = max(dist_tags, key=lambda x: int(x.get('confidence') or 0))
            ev = '；'.join(str(x) for x in (t.get('evidence') or [])[:4])
            advice_text += f'风险相位: 回避·出货相（置信 {int(t.get("confidence") or 0)}/10）'
            if ev:
                advice_text += f' — {ev}'
            advice_text += '\n'
        if invalidation:
            advice_text += f'失效条件: {invalidation}\n'
        trade_setup = pred.get('_trade_setup') or {}
        if isinstance(trade_setup, dict) and trade_setup:
            code = pred.get('code')
            _setup_label = {
                'trend_breakout': '趋势突破',
                'box_breakout': '箱体突破',
                'pullback_buy': '上升回踩',
                'oversold_rebound': '超跌反弹',
                'event_momentum': '事件催化',
                'downtrend_avoid': '下跌回避',
            }.get(trade_setup.get('setup_name'), trade_setup.get('setup_name', '—'))
            _status_label = {
                'candidate': '候选未触发',
                'triggered': '已触发',
                'failed': '已失败',
                'avoid': '禁止买入',
            }.get(trade_setup.get('status'), trade_setup.get('status', '—'))
            advice_text += (
                f'短线模式: {_setup_label} / {_status_label}\n'
                f'触发价: {format_price_or_dash(trade_setup.get("entry_trigger"), code)}  '
                f'失效价: {format_price_or_dash(trade_setup.get("fail_level"), code)}  '
                f'仓位: {trade_setup.get("position_hint", "—")}\n'
            )
        opportunity = pred.get('_opportunity_profile') or {}
        if isinstance(opportunity, dict) and opportunity:
            gaps = '、'.join(opportunity.get('evidence_gaps') or []) or '无'
            advice_text += (
                f'机会等级: {opportunity.get("opportunity_label") or "—"}  '
                f'阶段: {opportunity.get("setup_phase") or "—"}  '
                f'风险收益比: {opportunity.get("risk_reward") if opportunity.get("risk_reward") is not None else "—"}\n'
                f'未持仓: {opportunity.get("not_holding_plan") or "—"}\n'
                f'已持仓: {opportunity.get("holding_plan") or "—"}\n'
                f'进攻条件: {opportunity.get("attack_level") or "—"}\n'
                f'防守条件: {opportunity.get("defense_level") or "—"}\n'
                f'证据缺口: {gaps}\n'
            )
        if pred.get('_trade_block_reason'):
            advice_text += f'交易限制: {pred["_trade_block_reason"]}，仅建立观察追踪，不建议买入\n'
        if pred.get('_holding_advice'):
            advice_text += f'持仓建议: {pred["_holding_advice"]}\n'
        if thesis_full:
            advice_text += f'\n{thesis_full}'

        cache_hint = ''
        if pred.get('from_reasoning_cache'):
            cache_hint = '  ⚡ 推理缓存命中（4h内）'

        from core.agents.base import agent_dict_to_html
        _EMPTY = '（暂无内容）'
        ref_ids = pred.get('_intel_ref_ids') or {}
        # 5 agent sections 用 HTML 渲染（intel_refs 可点击）
        for sec, raw in [
            (self._sec_macro, agent_mac),
            (self._sec_company, agent_cmp),
            (self._sec_tech, agent_tec),
            (self._sec_fundflow, agent_fund),
            (self._sec_news, agent_news),
        ]:
            d = raw
            if isinstance(d, str) and d.strip().startswith('{'):
                try:
                    import json as _j
                    parsed = _j.loads(d)
                    if isinstance(parsed, dict) and 'stance' in parsed:
                        d = parsed
                except Exception:
                    pass
            if isinstance(d, dict):
                html_out = agent_dict_to_html(d, ref_ids) or _EMPTY
                if sec is self._sec_tech:
                    html_out += _behavior_tags_html(_behavior_tags_of(pred))
                if sec is self._sec_news:
                    html_out = _intel_digest_html(pred.get('_intel_digest')) + html_out
                sec.update_html(html_out)
            else:
                text_out = str(d) if d else _EMPTY
                if sec is self._sec_news and pred.get('_intel_digest'):
                    text_out = f'本次纳入 {len(pred["_intel_digest"])} 条情报\n\n' + text_out
                sec.update_text(text_out)
        self._sec_debate.update_text(debate if debate and not debate.startswith('（') else _EMPTY)
        self._sec_decision.update_text(g_text + cache_hint)
        self._sec_advice.update_text(advice_text)

        # 追踪任务在预测完成后自动创建；这里不再提供手动买入/虚拟买入入口
        self._buy_btn.setVisible(False)
        self._skip_btn.setVisible(True)
        self._copy_btn.setVisible(True)

    def clear_analysis(self):
        """清除分析内容，保留 DetailStrip（持仓选择状态不变）。"""
        self._pred = None
        self._reasoning = ''
        for attr, _, _ in self._SECTION_CONFIG:
            sec = getattr(self, attr, None)
            if sec:
                sec.update_text('（暂无内容）')
        self._hide_analysis_buttons()
        self._empty_lbl.setVisible(True)

    def clear(self):
        """完全清空（无选中时调用）。"""
        self._strip.clear()
        self._sec_kline.setVisible(False)
        self._kline_canvas.render([])
        self.clear_analysis()

    def _on_buy(self):
        """已废弃：追踪任务在预测完成后自动创建，不再需要手动买入。"""
        if self._pred:
            self.virtual_buy_requested.emit(self._pred)

    def _on_copy(self):
        if not self._pred:
            return
        pred = self._pred
        code = pred.get('code')
        def _setup_text(setup: dict) -> str:
            if not isinstance(setup, dict) or not setup:
                return '（无内容）'
            setup_name = {
                'trend_breakout': '趋势突破',
                'box_breakout': '箱体突破',
                'pullback_buy': '上升回踩',
                'oversold_rebound': '超跌反弹',
                'event_momentum': '事件催化',
                'downtrend_avoid': '下跌回避',
            }.get(setup.get('setup_name'), setup.get('setup_name') or '—')
            status = {
                'candidate': '候选未触发',
                'triggered': '已触发',
                'failed': '已失败',
                'avoid': '禁止买入',
            }.get(setup.get('status'), setup.get('status') or '—')
            risk = '、'.join(setup.get('risk_flags') or []) or '无'
            return '\n'.join([
                f'模式：{setup_name}',
                f'状态：{status}',
                f'触发价：{format_price_or_dash(setup.get("entry_trigger"), code)}',
                f'失效价：{format_price_or_dash(setup.get("fail_level"), code)}',
                f'目标价：{format_price_or_dash(setup.get("target_level"), code)}',
                f'建议周期：{setup.get("suggested_horizon_days") or "—"}个交易日',
                f'仓位提示：{setup.get("position_hint") or "—"}',
                f'原因：{setup.get("reason") or "—"}',
                f'风险标记：{risk}',
            ])

        def _opportunity_text(opportunity: dict) -> str:
            if not isinstance(opportunity, dict) or not opportunity:
                return '（无内容）'
            gaps = '、'.join(opportunity.get('evidence_gaps') or []) or '无'
            risks = '、'.join(opportunity.get('risk_flags') or []) or '无'
            evidence = '、'.join(opportunity.get('evidence') or []) or '无'
            rr = opportunity.get('risk_reward')
            return '\n'.join([
                f'机会等级：{opportunity.get("opportunity_label") or "—"}',
                f'当前阶段：{opportunity.get("setup_phase") or "—"}',
                f'触发距离：{opportunity.get("trigger_distance_pct") if opportunity.get("trigger_distance_pct") is not None else "—"}%',
                f'风险收益比：{rr if rr is not None else "—"}',
                f'未持仓策略：{opportunity.get("not_holding_plan") or "—"}',
                f'已持仓策略：{opportunity.get("holding_plan") or "—"}',
                f'加仓条件：{opportunity.get("add_condition") or "—"}',
                f'减仓条件：{opportunity.get("reduce_condition") or "—"}',
                f'退出条件：{opportunity.get("exit_condition") or "—"}',
                f'进攻条件：{opportunity.get("attack_level") or "—"}',
                f'防守条件：{opportunity.get("defense_level") or "—"}',
                f'正面证据：{evidence}',
                f'风险标记：{risks}',
                f'证据缺口：{gaps}',
            ])

        sections = [
            ('市场环境Agent', pred.get('_agent_macro')),
            ('基本面Agent', pred.get('_agent_company')),
            ('技术面Agent', pred.get('_agent_technical') or pred.get('_agent_tech')),
            ('资金流向Agent', pred.get('_agent_fundflow')),
            ('事件催化Agent', pred.get('_agent_news')),
        ]
        lines = [
            f'【Agent输出】{pred.get("name")}（{pred.get("code")}） {pred.get("created_at","")[:16]}',
        ]
        for title, raw in sections:
            body = _zh_text(self._fmt_agent(raw)).strip() or '（无内容）'
            lines.extend(['', '=' * 40, f'【{title}】', body])
            if title == '技术面Agent':
                bt_text = _behavior_tags_text(_behavior_tags_of(pred))
                if bt_text:
                    lines.extend(['', '— 技术行为标签（代码量化生成 · 仅疑似）—', bt_text])
        trade_setup = pred.get('_trade_setup')
        if trade_setup:
            lines.extend(['', '=' * 40, '【确定性短线模式】', _setup_text(trade_setup)])
        opportunity = pred.get('_opportunity_profile')
        if opportunity:
            lines.extend(['', '=' * 40, '【确定性机会分层】', _opportunity_text(opportunity)])
        lines.extend([
            '',
            '=' * 40,
            '【F层 多空辩论】',
            _zh_text(pred.get('_debate') or '（无内容）').strip() or '（无内容）',
            '',
            '=' * 40,
            '【Z层 周期约束】',
            _zh_text(pred.get('_horizon_directive') or '（无内容）').strip() or '（无内容）',
            '',
            '=' * 40,
            '【G层 最终决策】',
            _zh_text(pred.get('thesis') or pred.get('reasoning') or '（无内容）').strip() or '（无内容）',
            '',
            '=' * 40,
            '【操作字段】',
            '\n'.join(format_prediction_copy_field_lines(pred)),
            '',
            '=' * 40,
            '【原始思考】',
            _zh_text(self._reasoning or '（无内容）').strip() or '（无内容）',
        ])
        QApplication.clipboard().setText('\n'.join(lines))
        self._copy_btn.setText('已复制')
        QTimer.singleShot(2000, lambda: self._copy_btn.setText('复制全文'))

# =========================================================
# _BatchAnalyzeDialog — 勾选持仓批量分析
# =========================================================
class _BatchAnalyzeDialog(QDialog):
    """勾选要批量分析的持仓股票；底部显示并发估算耗时。"""
    _PER_STOCK_MIN = 2  # 单只预计分钟
    _MAX_PARALLEL = 2

    @staticmethod
    def _estimate_batch_minutes(count: int, slots: int = _MAX_PARALLEL) -> int:
        if count <= 0:
            return 0
        slots = max(1, slots)
        return max(_BatchAnalyzeDialog._PER_STOCK_MIN,
                   -(-count // slots) * _BatchAnalyzeDialog._PER_STOCK_MIN)

    def __init__(self, holdings: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle('一键分析持仓')
        self.resize(360, 460)
        self.setStyleSheet('background:#0d0d1f; color:#e0e0e0;')
        self._checks: list[tuple[QCheckBox, dict]] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        tip = QLabel('勾选要分析的股票，点「开始分析」后后台最多 2 只同时分析，\n'
                     '完成的会自动建立追踪任务，可去「追踪」页查看。')
        tip.setStyleSheet('color:#8899aa; font-size:12px;')
        lay.addWidget(tip)

        bar = QHBoxLayout()
        all_btn = QPushButton('全选')
        none_btn = QPushButton('清空')
        for b in (all_btn, none_btn):
            b.setFixedHeight(24)
            b.setStyleSheet('QPushButton { background:#1a2a3a; color:#aabbcc; border:none;'
                            ' border-radius:3px; font-size:11px; padding:0 12px; }'
                            'QPushButton:hover { background:#2a3a5a; color:#fff; }')
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        bar.addWidget(all_btn)
        bar.addWidget(none_btn)
        bar.addStretch()
        lay.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet('QScrollArea { background:#0a111f; border:1px solid #1a2a4a;'
                             ' border-radius:4px; }')
        inner = QWidget()
        inner.setStyleSheet('background:#0a111f;')
        in_lay = QVBoxLayout(inner)
        in_lay.setContentsMargins(8, 6, 8, 6)
        in_lay.setSpacing(2)
        for h in holdings:
            code = h.get('code', '')
            name = h.get('name', '') or code
            if not code:
                continue
            cb = QCheckBox(f'{name}（{code}）')
            cb.setChecked(True)
            cb.setStyleSheet('QCheckBox { color:#d8e4f0; font-size:12px; padding:3px; }')
            cb.toggled.connect(self._update_estimate)
            in_lay.addWidget(cb)
            self._checks.append((cb, h))
        in_lay.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

        self._est_lbl = QLabel()
        self._est_lbl.setStyleSheet('color:#f0c040; font-size:12px; font-weight:bold;')
        lay.addWidget(self._est_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton('取消')
        cancel_btn.setStyleSheet('QPushButton { background:#2a2a3a; color:#aaa; border:none;'
                                 ' border-radius:4px; padding:6px 18px; }')
        cancel_btn.clicked.connect(self.reject)
        self._ok_btn = QPushButton('开始分析')
        self._ok_btn.setStyleSheet('QPushButton { background:#2d5bd7; color:#fff; font-weight:bold;'
                                   ' border:none; border-radius:4px; padding:6px 20px; }'
                                   'QPushButton:hover { background:#3a6bf0; }')
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._ok_btn)
        lay.addLayout(btn_row)

        self._update_estimate()

    def _set_all(self, val: bool):
        for cb, _ in self._checks:
            cb.setChecked(val)

    def _update_estimate(self, *_):
        n = sum(1 for cb, _ in self._checks if cb.isChecked())
        est = self._estimate_batch_minutes(n)
        self._est_lbl.setText(f'已选 {n} 只 · 预计约 {est} 分钟（最多 2 只同时分析，每只约 2 分钟）')
        self._ok_btn.setEnabled(n > 0)

    def selected(self) -> list[dict]:
        return [h for cb, h in self._checks if cb.isChecked()]


# =========================================================
# PortfolioPanel — 主面板（三段式垂直布局）
# =========================================================
class PortfolioPanel(QWidget):
    status_changed        = Signal(str, str)
    point_count_changed   = Signal(int)
    bottom_status_changed = Signal(str)
    tracking_task_created = Signal()
    tracking_tasks_settled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._intel_events: list      = []
        self._sector_context: dict    = {}
        self._emotion_context: dict | None = None
        self._global_context: dict | None  = None
        self._current_prices: dict    = {}
        self._worker: _PredictWorker | None  = None
        # 批量分析（后台队列，最高并发 2）状态
        self._batch_running = False
        self._batch_slots = 2                     # 最高同时分析只数
        self._batch_active: set = set()           # 进行中的 worker
        self._batch_queue: list[dict] = []
        self._batch_total = 0
        self._batch_started = 0
        self._batch_done = 0
        self._batch_failed: list[str] = []
        self._batch_tracking_created = 0
        self._batch_tracking_skipped: list[str] = []
        self._kline_worker: _KlineWorker | None = None
        self._price_worker: _PriceRefreshWorker | None = None
        self._current_pred: dict | None = None
        self._bought_ids: set[str]    = set()
        self.alert_center             = None
        self._selected_holding: dict | None = None
        self._total_real_value: float = 0.0
        self._tracking_tasks: list[dict] = load_tasks()
        self._drawer: _CalendarDrawer | None = None
        self._pnl_freeze_attempts: dict[str, int] = {}  # {date: 已尝试次数}
        self._pnl_intraday_enabled = True
        self._build_ui()
        self._refresh_list()
        # 盘后收益冻结懒启动：启动后延迟进入队列，避免打开软件时抢占 UI/网络资源。
        self._pnl_freeze_timer = QTimer(self)
        self._pnl_freeze_timer.setInterval(15000)
        self._pnl_freeze_timer.timeout.connect(self._pnl_freeze_tick)
        QTimer.singleShot(45000, self._start_pnl_freeze_timer)

    # ── 构建 UI ──────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ① 顶栏 32px：添加持仓
        top_bar = QWidget()
        top_bar.setFixedHeight(32)
        top_bar.setStyleSheet('background:#0f1a2e; border-bottom:1px solid #1a2a3a;')
        tb_lay = QHBoxLayout(top_bar)
        tb_lay.setContentsMargins(8, 2, 8, 2)
        tb_lay.setSpacing(8)
        tb_lay.addStretch(1)
        self._add_btn = QPushButton('➕ 添加持仓')
        self._add_btn.setFixedHeight(24)
        self._add_btn.setStyleSheet(
            'QPushButton { background:#1a3a1a; color:#7ef08e; border:none;'
            ' border-radius:3px; font-size:12px; padding:0 8px; }'
            'QPushButton:hover { background:#1f5a1f; }'
        )
        self._add_btn.clicked.connect(self._on_add_holding)
        tb_lay.addWidget(self._add_btn)

        # 一键分析持仓：勾选→后台依次分析→自动建追踪
        self._btn_batch = QPushButton('🔍 一键分析')
        self._btn_batch.setFixedHeight(24)
        self._btn_batch.setStyleSheet(
            'QPushButton { background:#3a2a5a; color:#c8b0f0; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 8px; }'
            'QPushButton:hover { background:#5a3a8a; color:#fff; }'
        )
        self._btn_batch.setToolTip('勾选持仓股票后台最多 2 只同时 AI 分析，每只约 2 分钟，\n完成自动建追踪任务，可去「追踪」页查看')
        self._btn_batch.clicked.connect(self._on_batch_analyze)
        tb_lay.addWidget(self._btn_batch)

        self._cal_btn = QPushButton('📅 月历')
        self._cal_btn.setFixedHeight(24)
        self._cal_btn.setStyleSheet(
            'QPushButton { background:#1a2a3a; color:#aabbcc; border:1px solid #2a3a4a;'
            ' border-radius:3px; font-size:11px; padding:0 8px; }'
            'QPushButton:hover { background:#2a3a5a; color:#fff; }'
            'QPushButton:checked { background:#e94560; color:#fff; border-color:#e94560; }'
        )
        self._cal_btn.setCheckable(True)
        self._cal_btn.clicked.connect(self._toggle_calendar)
        tb_lay.addWidget(self._cal_btn)

        root.addWidget(top_bar)

        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(8)
        self._main_splitter.setStyleSheet(
            'QSplitter::handle:vertical { background:#142238; margin:2px 0; }'
            'QSplitter::handle:vertical:hover { background:#2c4f78; }'
        )

        upper_area = QWidget()
        upper_lay = QVBoxLayout(upper_area)
        upper_lay.setContentsMargins(0, 0, 0, 0)
        upper_lay.setSpacing(0)

        # ② 持仓表格
        self._table = _HoldingsTable()
        self._table.setMinimumHeight(180)
        self._table.row_selected.connect(self._on_table_row_selected)
        self._table.edit_requested.connect(self._on_edit_holding)
        upper_lay.addWidget(self._table, stretch=1)

        # ③ 分隔提示行 24px
        self._separator_label = QLabel('▼ 选中：—')
        self._separator_label.setFixedHeight(24)
        self._separator_label.setStyleSheet(
            'background:#0a1020; color:#445566; font-size:11px;'
            ' padding:0 12px; border-top:1px solid #1a2a3a;'
            ' border-bottom:1px solid #1a2a3a;'
        )
        upper_lay.addWidget(self._separator_label)

        # ④ 分析区（占满剩余，内含 strip + K线 + Agent sections）
        self._agent_panel = _AgentDetailPanel()
        self._analysis_view = self._agent_panel  # 供 main_window 连接 intel_anchor_clicked
        strip = self._agent_panel.strip
        strip.ai_analyze_requested.connect(self._on_ai_from_strip)
        strip.force_refresh_requested.connect(
            lambda c, n: self._run_prediction(c, n, force_refresh=True)
        )
        strip.delete_requested.connect(self._on_delete_holding)
        self._agent_panel.virtual_buy_requested.connect(self._on_virtual_buy)
        self._agent_panel.kline_canvas.period_changed.connect(
            self._on_kline_period_changed
        )
        self._main_splitter.addWidget(upper_area)
        self._main_splitter.addWidget(self._agent_panel)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 3)
        self._main_splitter.setSizes([300, 700])
        root.addWidget(self._main_splitter, stretch=1)

    def _toggle_calendar(self):
        if self._drawer is None:
            self._drawer = _CalendarDrawer(self)
            self._drawer.closed.connect(lambda: self._cal_btn.setChecked(False))
        if not self._drawer._open:
            records = load_holdings_pnl_calendar()
            self._drawer.set_records(records)
        self._drawer.toggle()
        self._cal_btn.setChecked(self._drawer._open)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._drawer is not None and self._drawer._open:
            self._drawer.reposition()

    def reapply_font(self):
        """MainWindow calls this after changing global font size."""
        if hasattr(self, '_agent_panel') and self._agent_panel is not None:
            self._agent_panel.reapply_font()

    # ── 列表刷新 ─────────────────────────────────────────────
    def _refresh_list(self):
        holdings = load_holdings()
        self.point_count_changed.emit(len(holdings))
        total_val = 0.0
        for h in holdings:
            cost   = h.get('cost_price', 0) or 0
            shares = h.get('shares', 0) or 0
            cur    = (self._current_prices.get(h.get('code', ''), {}).get('price')) or cost
            total_val += (cur or cost) * shares
        self._total_real_value = total_val
        self._table.load_real(holdings, self._current_prices, self._intel_events)

    # ── 表格行选中：中央调度 ─────────────────────────────────
    def _on_table_row_selected(self, data: dict, _is_virtual: bool):
        self._selected_holding = data
        code = data.get('code', '')
        name = data.get('name', '')

        self._separator_label.setText(f'▼ 选中：{name}（{code}）')

        cur = self._current_prices.get(code, {}).get('price')
        self._agent_panel.show_holding(data, cur, total_val=self._total_real_value)

        # 有缓存预测则展示，同时尝试渲染缓存 K 线；否则自动拉取 K 线
        cached = _find_cached_pred(code)
        if cached:
            already = cached.get('prediction_id', '') in self._bought_ids
            self._agent_panel.show_prediction(
                cached, cached.pop('_reasoning', ''), already,
            )
            kline_data = cached.get('_kline_data', [])
            if kline_data:
                self._agent_panel.kline_canvas.render(kline_data, code)
            else:
                self._on_fetch_kline(code, name)
        else:
            self._agent_panel.clear_analysis()
            self._on_fetch_kline(code, name)

    # ── 添加持仓 ─────────────────────────────────────────────
    def _on_add_holding(self):
        dlg = _HoldingDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.get_data()
        try:
            add_holding(data)
        except ValueError as e:
            QMessageBox.warning(self, '无法添加', str(e))
            return
        self._refresh_list()
        now = datetime.now().strftime('%H:%M:%S')
        self.bottom_status_changed.emit(
            f'已添加持仓 {data["name"]}（{data["code"]}）{now}'
        )
        self.status_changed.emit('success', f'已添加 {data["name"]}')

    # ── 编辑持仓 ─────────────────────────────────────────────
    def _on_edit_holding(self, h: dict):
        code = h.get('code', '')
        cur_quote = self._current_prices.get(code, {}) if isinstance(self._current_prices, dict) else {}
        current_price = cur_quote.get('price') or cur_quote.get('current_price')
        dlg = _HoldingDialog(self, prefill=h, current_price=current_price)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.get_data()
        hid = h.get('id')
        if not hid:
            return
        update_holding(hid,
            code=data['code'], name=data['name'], cost_price=data['cost_price'],
            shares=data['shares'],
        )
        self._refresh_list()
        now = datetime.now().strftime('%H:%M:%S')
        mode_label = {'buy': '买入更新', 'sell': '卖出更新'}.get(data.get('mode'), '已更新')
        self.bottom_status_changed.emit(
            f'{mode_label}持仓 {data["name"]}（{data["code"]}）{now}'
        )

    # ── 删除持仓 ─────────────────────────────────────────────
    def _on_delete_holding(self, hid: str):
        remove_holding(hid)
        self._separator_label.setText('▼ 选中：—')
        self._agent_panel.clear()
        self._selected_holding = None
        self._refresh_list()

    # ── K 线周期切换 ─────────────────────────────────────────
    def _on_kline_period_changed(self, days: int):
        if not self._selected_holding:
            return
        code = self._selected_holding.get('code', '')
        name = self._selected_holding.get('name', '')
        if not code:
            return
        if self._kline_worker and self._kline_worker.isRunning():
            return
        self.status_changed.emit('loading', f'{name}: 切换 {days}日K线…')
        self._kline_worker = _KlineWorker(code, parent=self, days=days)

        def _on_done(rows, err, _n=name, _d=days):
            if err and not rows:
                self.status_changed.emit('error', f'{_n}: K线拉取失败')
                return
            self._agent_panel.kline_canvas.render(rows, code)
            self.status_changed.emit('ok', f'{_n}: 已切换 {_d}日K线')

        self._kline_worker.finished.connect(_on_done)
        self._kline_worker.start()

    def _on_fetch_kline(self, code: str, name: str):
        if self._kline_worker and self._kline_worker.isRunning():
            return
        self._agent_panel.strip.set_ai_enabled(False)
        self.status_changed.emit('loading', f'{name}: 正在拉取K线…')
        days = self._agent_panel.kline_canvas.current_period()
        self._kline_worker = _KlineWorker(code, parent=self, days=days)

        def _on_done(rows, err, _c=code, _n=name):
            self._agent_panel.strip.set_ai_enabled(True)
            if err and not rows:
                QMessageBox.warning(self, 'K线拉取失败', f'{_n}（{_c}）\n{err}')
                self.status_changed.emit('error', f'{_n}: K线拉取失败')
                return
            self._agent_panel.kline_canvas.render(rows, code)
            self.status_changed.emit('success', f'{_n}: K线已加载（{len(rows)}根）')

        self._kline_worker.finished.connect(_on_done)
        self._kline_worker.start()

    # ── AI 分析（从 DetailStrip 按钮触发）────────────────────
    def _on_ai_from_strip(self, code: str, name: str):
        self._run_prediction(code, name, force_refresh=True)

    # ── 生成预测（独立输入对话框）────────────────────────────
    def _on_gen_predict(self):
        if self._worker and self._worker.isRunning():
            return
        prefill_code = prefill_name = ''
        if self._selected_holding:
            prefill_code = self._selected_holding.get('code', '')
            prefill_name = self._selected_holding.get('name', '')

        api_key = _load_api_key()
        if not api_key:
            key, ok = QInputDialog.getText(
                self, 'DeepSeek API Key',
                '请输入 DeepSeek API Key（仅保存本地）：',
                QLineEdit.EchoMode.Normal,
            )
            if not ok or not key.strip():
                return
            api_key = key.strip()
            _save_api_key(api_key)

        dlg = _StockPickerDialog(self, prefill_code=prefill_code, prefill_name=prefill_name)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        code, name = dlg.get_input()
        self._run_prediction(code, name)

    def _run_prediction(self, code: str, name: str, force_refresh: bool = True):
        if self._batch_running:
            QMessageBox.information(self, '批量分析进行中', '正在批量分析持仓，请等队列完成后再单独分析。')
            return
        if self._worker and self._worker.isRunning():
            return
        api_key = _load_api_key()
        if not api_key:
            key, ok = QInputDialog.getText(
                self, 'DeepSeek API Key',
                '请输入 DeepSeek API Key（仅保存本地）：',
                QLineEdit.EchoMode.Normal,
            )
            if not ok or not key.strip():
                return
            api_key = key.strip()
            _save_api_key(api_key)

        intel_snapshot  = list(self._intel_events)
        sector_snapshot = dict(self._sector_context)
        context: dict   = {}
        if self._emotion_context:
            context['emotion'] = dict(self._emotion_context)
        if self._global_context:
            context['global']  = dict(self._global_context)
        current_price = self._current_prices.get(code, {}).get('price')
        selected = self._selected_holding if isinstance(self._selected_holding, dict) else None
        selected_holding = selected if selected and selected.get('code') == code else None
        cognition = None
        cog_dlg = _CognitionDialog(self, selected_holding, current_price)
        if cog_dlg.exec() == QDialog.DialogCode.Accepted:
            cognition = cog_dlg.get_data()

        self._agent_panel.strip.set_ai_enabled(False)
        label = '强制重新分析' if force_refresh else '准备数据 + AI推理'
        self.status_changed.emit('loading', f'{name}: {label}…（约 2 分钟）')

        self._worker = _PredictWorker(
            code, name, api_key,
            intel_snapshot, context, sector_snapshot,
            current_price, force_refresh=force_refresh, cognition=cognition, parent=self,
        )
        self._worker.status_update.connect(
            lambda msg: self.status_changed.emit('loading', f'{name}: {msg}')
        )
        self._worker.finished.connect(self._on_predict_done)
        self._worker.start()

    def _on_predict_done(self, pred, reasoning, cache_key, error):
        self._agent_panel.strip.set_ai_enabled(True)

        if error or pred is None:
            QMessageBox.warning(self, '预测失败', error or '未知错误')
            self.status_changed.emit('error', f'预测失败: {error}')
            return

        self._current_pred = pred
        self._agent_panel.show_prediction(pred, reasoning, already_bought=False)
        kline_data = pred.get('_kline_data', [])
        if kline_data:
            self._agent_panel.kline_canvas.render(kline_data, pred.get('code', ''))

        # 自动创建追踪任务
        try:
            new_task = create_task(pred, self._intel_events)
            self._tracking_tasks.append(new_task)
            self.tracking_task_created.emit()
            self.bottom_status_changed.emit(
                f'追踪任务已创建 {new_task["id"]} 审判日 {new_task["deadline"]}'
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning('[tracking] 创建任务失败: %s', e)
            self.bottom_status_changed.emit(f'追踪建单被拒: {e}')

        self._refresh_list()
        now = datetime.now().strftime('%H:%M:%S')
        self.status_changed.emit('success', f'{pred.get("name")} 预测完成 {now}')
        self.bottom_status_changed.emit(f'预测面板已更新 {now}')

    # ── 一键批量分析（后台顺序队列）──────────────────────────────────────────
    def _on_batch_analyze(self):
        if self._batch_running:
            QMessageBox.information(self, '批量分析进行中',
                                    f'已完成 {self._batch_done}/{self._batch_total}，请等队列跑完。')
            return
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, '请稍候', '当前有单只分析正在进行，完成后再批量分析。')
            return
        holdings = load_holdings()
        if not holdings:
            QMessageBox.information(self, '无持仓', '当前没有真实持仓可分析。')
            return
        api_key = _load_api_key()
        if not api_key:
            QMessageBox.warning(self, '缺少 API Key', '请先在「数据管理」填入 DeepSeek API Key。')
            return
        dlg = _BatchAnalyzeDialog(holdings, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        items = dlg.selected()
        if not items:
            return
        est = _BatchAnalyzeDialog._estimate_batch_minutes(len(items), self._batch_slots)
        if QMessageBox.question(
            self, '确认批量分析',
            f'将分析 {len(items)} 只股票（后台最多 2 只同时跑），预计约 {est} 分钟。\n'
            f'分析在后台进行，期间软件可正常使用；完成的会自动建立追踪任务。\n开始？',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return

        # 一次性快照共享上下文（队列全程复用，避免反复读主线程状态）
        self._batch_api_key = api_key
        self._batch_intel = list(self._intel_events)
        self._batch_sector = dict(self._sector_context)
        ctx: dict = {}
        if self._emotion_context:
            ctx['emotion'] = dict(self._emotion_context)
        if self._global_context:
            ctx['global'] = dict(self._global_context)
        self._batch_ctx = ctx

        self._batch_queue = list(items)
        self._batch_total = len(items)
        self._batch_started = 0
        self._batch_done = 0
        self._batch_failed = []
        self._batch_tracking_created = 0
        self._batch_tracking_skipped = []
        self._batch_active = set()
        self._batch_running = True
        self._btn_batch.setText('⏳ 分析中…')
        self._btn_batch.setEnabled(False)
        self._agent_panel.strip.set_ai_enabled(False)
        self._batch_pump()

    def _batch_pump(self):
        """填满空闲槽位（最高并发 self._batch_slots），队列与在跑都清空则收尾。"""
        while self._batch_queue and len(self._batch_active) < self._batch_slots:
            self._batch_start_one()
        if not self._batch_queue and not self._batch_active:
            self._batch_finish()
            return
        self._batch_emit_progress()

    def _batch_start_one(self):
        h = self._batch_queue.pop(0)
        code = h.get('code', '')
        name = h.get('name', '') or code
        self._batch_started += 1
        current_price = self._current_prices.get(code, {}).get('price')
        worker = _PredictWorker(
            code, name, self._batch_api_key,
            self._batch_intel, dict(self._batch_ctx), self._batch_sector,
            current_price, force_refresh=True, cognition=None, parent=self,
        )
        worker.finished.connect(self._batch_on_done)
        self._batch_active.add(worker)
        worker.start()

    def _batch_emit_progress(self):
        done = self._batch_done + len(self._batch_failed)
        active = len(self._batch_active)
        remain = len(self._batch_queue) + active
        remain_min = max(1, -(-remain // self._batch_slots) * 2)  # ceil(remain/2)×2
        self.status_changed.emit(
            'loading',
            f'批量分析：完成 {done}/{self._batch_total}，进行中 {active}（约剩 {remain_min} 分钟）…')

    def _batch_on_done(self, pred, reasoning, cache_key, error):
        w = self.sender()
        self._batch_active.discard(w)
        if w is not None:
            w.deleteLater()
        if error or pred is None:
            name = getattr(w, '_name', '') if w is not None else ''
            msg = str(error or '未知错误')
            self._batch_failed.append(f'{name}: {msg}' if name else msg)
        else:
            self._batch_done += 1
            try:
                new_task = create_task(pred, self._intel_events)
                self._tracking_tasks.append(new_task)
                self.tracking_task_created.emit()
                self._batch_tracking_created += 1
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('[batch] 建追踪失败: %s', e)
                self._batch_tracking_skipped.append(
                    f'{pred.get("name","")}: 未建追踪({e})'
                )
        self._batch_pump()

    def _batch_finish(self):
        self._batch_running = False
        self._batch_active = set()
        self._btn_batch.setText('🔍 一键分析')
        self._btn_batch.setEnabled(True)
        self._agent_panel.strip.set_ai_enabled(True)
        self._refresh_list()
        now = datetime.now().strftime('%H:%M:%S')
        fail_n = len(self._batch_failed)
        skipped_n = len(self._batch_tracking_skipped)
        msg = (
            f'批量分析完成：分析成功 {self._batch_done} 只，失败 {fail_n} 只；'
            f'已建立追踪任务 {self._batch_tracking_created} 个。'
        )
        if skipped_n:
            detail = '\n'.join(self._batch_tracking_skipped[:15])
            if skipped_n > 15:
                detail += f'\n…等共 {skipped_n} 条'
            msg += (
                f'\n\n分析成功但未建立追踪 {skipped_n} 只'
                f'（新鲜度/建单守卫仍然生效）：\n{detail}'
            )
        if fail_n:
            detail = '\n'.join(self._batch_failed[:15])
            if fail_n > 15:
                detail += f'\n…等共 {fail_n} 条'
            msg += f'\n\n分析失败 {fail_n} 只：\n{detail}'
        self.status_changed.emit('success', f'批量分析完成 {now}')
        QMessageBox.information(self, '批量分析完成', msg)

    # ── 旧虚拟买入逻辑（已废弃，追踪任务自动创建）────────────────────────────────
    def _on_virtual_buy(self, pred: dict):
        """已废弃：追踪任务在预测完成后自动创建，不再需要手动虚拟买入。"""
        pid = pred.get('prediction_id', '')
        if pid in self._bought_ids:
            return
        try:
            vp_id = add_virtual_position(pred)
            self._bought_ids.add(pid)
            self.bottom_status_changed.emit(f'已记录追踪任务 {vp_id}')
            self._update_stats()
            self._agent_panel.show_prediction(pred, '', already_bought=True)
            if self.alert_center:
                direction_text = _zh_text(pred.get('direction') or '')
                self.alert_center.try_trigger(
                    event_type='virtual_open',
                    title='追踪任务已记录',
                    message=(
                        f'{pred.get("name")}（{pred.get("code")}）'
                        f'{direction_text}预测已记录，'
                        f'置信度 {pred.get("confidence")}/10'
                    ),
                    level='info',
                    key=f'virtual_open_{vp_id}',
                    cooldown=0,
                )
        except Exception as e:
            QMessageBox.warning(self, '记录失败', str(e))

    # ── 胜率统计 ─────────────────────────────────────────────
    def _update_stats(self):
        pass  # 统计展示移到 P3 追踪 Tab

    # ── 面板接口（供外部调用）────────────────────────────────
    def _start_pnl_freeze_timer(self):
        if not self._pnl_freeze_timer.isActive():
            self._pnl_freeze_timer.start()

    def enable_intraday_pnl_calendar(self):
        if self._pnl_intraday_enabled:
            return
        self._pnl_intraday_enabled = True
        try:
            from core.data_worker import is_trading_time
            if is_trading_time():
                QTimer.singleShot(500, self.refresh)
        except Exception:
            pass

    def refresh(self):
        self._tracking_tasks = load_tasks()
        self._refresh_list()
        if self._drawer is not None and self._drawer._open:
            self._drawer.set_records(load_holdings_pnl_calendar())
        # 后台拉一次实时价格
        holdings = load_holdings()
        codes = list({h['code'] for h in holdings if h.get('code')})
        option_codes = _holding_long_option_codes()
        if codes or option_codes:
            if self._price_worker and self._price_worker.isRunning():
                return
            if self._price_worker:
                try:
                    self._price_worker.finished.disconnect(self.set_current_prices)
                except Exception:
                    pass
            self._price_worker = _PriceRefreshWorker(codes, option_codes, parent=self)
            self._price_worker.finished.connect(self.set_current_prices)
            self._price_worker.start()

    def auto_refresh(self):
        from core.data_worker import is_trading_time
        if is_trading_time():
            self.refresh()

    def _pnl_freeze_tick(self):
        """盘后收益冻结驱动（懒启动后每 15s）：收盘后若当日未冻结则排队拉价。"""
        try:
            now = datetime.now()
            hm = now.hour * 100 + now.minute
            if not (is_trade_day(date.today()) and hm >= 1500):
                return
            today = date.today().strftime('%Y-%m-%d')
            holdings = load_holdings()
            try:
                from core.cache import load_option_watchlist
                opt_recs = load_option_watchlist()
            except Exception:
                opt_recs = []
            source_key = holdings_pnl_source_key(holdings=holdings, option_records=opt_recs)
            for r in load_holdings_pnl_calendar():
                if r.get('date') == today and frozen_snapshot_matches_sources(r, source_key):
                    return  # 今日已冻结，不再计算
            codes = [h['code'] for h in holdings if h.get('code')]
            option_codes = _holding_long_option_codes()
            if codes or option_codes:
                self.refresh()  # 异步拉收盘价 → 回调 set_current_prices → 计算
            else:
                self._update_holdings_pnl_calendar({})  # 空仓直接冻结
        except Exception:
            pass

    def set_intel_events(self, events: list):
        self._intel_events = list(events)

    def set_current_prices(self, prices: dict):
        self._current_prices = prices
        self._refresh_list()
        self._update_holdings_pnl_calendar(prices)
        if pending_maintenance_ids(self._tracking_tasks):
            self.tracking_tasks_settled.emit()

    def _update_holdings_pnl_calendar(self, prices: dict):
        now = datetime.now()
        today_date = date.today()
        today = today_date.strftime('%Y-%m-%d')
        phase = pnl_calendar_update_phase(
            now,
            is_trade_day=is_trade_day(today_date),
            intraday_enabled=self._pnl_intraday_enabled,
        )
        if phase is None:
            return
        if phase == 'live' and not prices:
            return

        holdings = load_holdings()
        try:
            from core.cache import load_option_watchlist
            opt_recs = load_option_watchlist()
        except Exception:
            opt_recs = []
        source_key = holdings_pnl_source_key(holdings=holdings, option_records=opt_recs)
        # 今日已冻结且来源未变则不再重算；收盘后新增持仓/期权时允许覆盖旧的空冻结。
        for r in load_holdings_pnl_calendar():
            if r.get('date') == today and frozen_snapshot_matches_sources(r, source_key):
                return
        snap = build_holdings_pnl_snapshot(
            holdings=holdings,
            option_records=opt_recs,
            prices=prices,
            today=today,
            frozen=False,
        )
        if phase == 'freeze':
            # 盘后冻结判定：所有持仓/期权都拿到价格即冻结；缺价则排队重试。
            # 为防个别永久缺价标的整夜重试，超过 40 次（约 10 分钟）冻结部分结果。
            self._pnl_freeze_attempts[today] = self._pnl_freeze_attempts.get(today, 0) + 1
            attempts = self._pnl_freeze_attempts[today]
            snap['frozen'] = (snap.get('missing_count', 0) == 0) or (attempts >= 40)
        else:
            self._pnl_freeze_attempts.pop(today, None)

        records = load_holdings_pnl_calendar()
        records = [r for r in records if r.get('date') != today]
        records.append(snap)
        save_holdings_pnl_calendar(records)
        if self._drawer is not None and self._drawer._open:
            self._drawer.set_records(records)

    def set_sector_context(self, context: dict):
        self._sector_context = context

    def set_emotion_context(self, snap: dict | None):
        self._emotion_context = snap

    def set_global_context(self, snap: dict | None):
        self._global_context = snap
