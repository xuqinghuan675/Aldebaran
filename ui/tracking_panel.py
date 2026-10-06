"""追踪任务顶级面板 — 个股 + 板块追踪的统一入口。"""
from __future__ import annotations

import logging
import time
from copy import deepcopy
from datetime import date, datetime, timedelta

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QApplication, QCheckBox, QComboBox, QFileDialog, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
    QWidget,
)

from core.credentials import load_api_key
from core.app_settings import load_setting, save_setting
from core.data_worker import is_trading_time
from core.data_source import fmt_price
from core.tracking_display import (
    format_agent_hit_lines, format_directional_metric_lines,
    format_open_review_rule_html, format_open_review_rule_text, format_prediction_plan_line,
    format_skill_kpi_html, sample_type_label, tracking_price_label,
)
from core.tracking import (
    build_retrospective_prompt, get_tracking_stats, load_tasks,
    benchmark_index_df, run_tracking_maintenance,
    export_tracking_report_docx, append_tracking_event,
    ensure_report_target_writable, delete_tasks, pending_maintenance_ids,
    persist_task_fields, _is_convertible_watch,
)
from ui.tracking_retro_scheduler import pending_retro_tasks

_GRADE_COLOR = {'A': '#2ecc71', 'B': '#1abc9c', 'C': '#f1c40f',
                'D': '#e67e22', 'F': '#e74c3c'}
_DIR_LONG_SET = {'多', 'bullish', 'long', '看多'}
_DIR_LABEL = {
    'bullish': '看多', 'bearish': '看跌回避', 'neutral': '中性',
    '多': '看多', '空': '看跌回避',
}
_PATH_QUALITY_LABEL = {
    'P_clean': '路径干净',
    'P_tested': '路径逼近',
    'P_stressed': '路径承压',
    'neutral': '观望路径',
}
_PATH_QUALITY_COLOR = {
    'P_clean': '#2ecc71',
    'P_tested': '#f1c40f',
    'P_stressed': '#e74c3c',
    'neutral': '#888888',
}
_AUTO_RETRO_WINDOW = timedelta(minutes=30)
_AUTO_RETRO_MAX_CONCURRENT = 1
_SETTLE_STARTUP_DELAY_MS = 3000
_SETTLE_BATCH = 8
_SETTLE_BATCH_GAP_MS = 1500
_CONVERT_COOLDOWN_S = 60
_AUTO_MAINTENANCE_INTERVAL_S = 600


def _delete_click_action(delete_multi_mode: bool, selected_count: int) -> str:
    if not delete_multi_mode:
        return 'enter_multi'
    if selected_count <= 0:
        return 'need_selection'
    return 'delete_selected'


def _is_long(direction: str) -> bool:
    return direction in _DIR_LONG_SET


def _pnl_pct(entry: float, current: float, direction: str) -> float:
    if entry <= 0:
        return 0.0
    raw = (current - entry) / entry * 100
    return raw if _is_long(direction) else -raw


def _is_unconverted_bullish_watch(task: dict) -> bool:
    bp = (task.get('pred_snapshot') or {}).get('task_blueprint') or {}
    return bp.get('category') == 'bullish_watch' and not task.get('converted_from_watch')


def _conversion_wait_text(task: dict, *, detailed: bool = False) -> str:
    reason = task.get('conversion_wait_reason')
    if not reason:
        return ''
    if isinstance(reason, str):
        return reason
    if not isinstance(reason, dict):
        return ''
    typ = str(reason.get('type') or '')
    if typ == 'volume_unconfirmed':
        return '⏳ 触价缩量，等待放量确认' if detailed else '⏳ 触价缩量，等放量'
    if typ == 'price_not_triggered':
        return '⏳ 未到触发价'
    if typ in ('kline_missing', 'kline_invalid'):
        return '⚠ K线异常/缺失，无法确认' if detailed else '⚠ K线异常/缺失'
    msg = str(reason.get('message') or '').strip()
    return msg


def _verdict_chip(task: dict) -> str:
    if task.get('status') != 'closed':
        return ''
    d = task.get('grade_detail') or {}
    g = task.get('grade') or ''
    if not g:
        return ''
    tc = d.get('task_class') or task.get('task_class') or ''
    if not tc:
        _dir = str(task.get('direction') or '')
        tc = 'avoid' if _dir == 'bearish' else ('buy' if _dir == 'bullish' else 'watch')
    if tc == 'buy':
        trig = d.get('trigger')
        if trig == 'target':
            return '✅ 止盈'
        if trig in ('stop', 'stop_same_day'):
            return '❌ 止损'
        return '✅ 兑现' if g in ('A', 'B', 'C') else '❌ 落空'
    if tc == 'avoid':
        return '✅ 避对' if g in ('A', 'B') else ('⚪ 中性' if g == 'C' else '❌ 踏空')
    if _is_unconverted_bullish_watch(task):
        if g in ('B', 'C'):
            return '✅ 观望合理'
        return '❌ 落空'
    if str(d.get('direction') or task.get('direction') or '') == 'bullish':
        return '✅ 兑现' if g in ('A', 'B', 'C') else '❌ 落空'
    return '✅ 观望合理' if g in ('A', 'B') else ('⚪ 中性' if g == 'C' else '❌ 错过')


# =========================================================
# 反思 Worker（结算后异步生成 AI 反思）
# =========================================================
class _RetrospectiveWorker(QThread):
    finished = Signal(str, str)
    error    = Signal(str, str)

    def __init__(self, task: dict, api_key: str, parent=None):
        super().__init__(parent)
        self._task    = task
        self._api_key = api_key

    def run(self):
        task_id = self._task.get('id', '')
        try:
            from core.cache import load_market_emotion_history
            from core.money_flow_provider import load_flow_history
            from core.agents.base import call_pro
            from core.analysis_memory import retrieve_recent_for_prompt
            flow_hist    = load_flow_history(self._task.get('code', ''), days=30)
            emotion_hist = load_market_emotion_history()
            if self._task.get('kind') == 'sector':
                mem_code = f'sector:{self._task.get("name") or self._task.get("code", "")}'
            else:
                mem_code = self._task.get('code', '')
            history_mem = retrieve_recent_for_prompt(mem_code)
            prompt = build_retrospective_prompt(
                self._task, flow_hist, emotion_hist, history_memory=history_mem)
            # deepseek-flash (V4.1 Flash) 为思考模型，reasoning 会先吃 token，需给足额度
            content, _ = call_pro(
                '你是一位专业 A 股量化分析师，正在对预测追踪任务做事后复盘反思。',
                prompt,
                self._api_key,
                max_tokens=3000,
                temperature=0.3,
            )
            text = (content or '').strip()
            if not text:
                self.error.emit(task_id, 'LLM 返回为空')
                return
            self.finished.emit(task_id, text)
        except Exception as e:
            self.error.emit(task_id, str(e))


class _WordExportWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        path: str,
        options: dict,
        include_ai_summary: bool,
        api_key: str,
        parent=None,
    ):
        super().__init__(parent)
        self._path = path
        self._options = dict(options)
        self._include_ai_summary = bool(include_ai_summary)
        self._api_key = api_key

    def run(self):
        try:
            result = export_tracking_report_docx(
                self._path,
                self._options,
                include_ai_summary=self._include_ai_summary,
                api_key=self._api_key,
            )
            self.completed.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class _SettlementWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str)

    def __init__(self, task_ids: set[str], prices: dict, parent=None):
        super().__init__(parent)
        self._task_ids = set(task_ids or set())
        self._prices = prices

    def run(self):
        try:
            result = run_tracking_maintenance(
                load_tasks(),
                self._prices,
                only_ids=self._task_ids,
            )
            self.completed.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class _RecomputeWorker(QThread):
    progress = Signal(int, int)
    completed = Signal(int, int)

    def __init__(self, tasks: list[dict], parent=None):
        super().__init__(parent)
        self._tasks = tasks

    def run(self):
        from core.kline_provider import fetch_daily_kline
        from core.tracking import backfill_triggered_watch_tasks, recompute_task, save_tasks_merge
        targets = [t for t in self._tasks
                   if t.get('status') == 'closed' and t.get('kind') != 'sector']
        total = len(targets)
        success = 0
        kline_cache = {}
        kline_days = {}
        for task in targets:
            try:
                created = date.fromisoformat(str(task.get('created_at'))[:10])
                deadline = date.fromisoformat(str(task.get('deadline'))[:10])
                span = (deadline - created).days + 10
                code = str(task.get('code', ''))
                kline_days[code] = max(kline_days.get(code, 0), max(span, 30))
            except Exception:
                pass
        for i, task in enumerate(targets):
            try:
                code = str(task.get('code', ''))
                if code not in kline_cache:
                    kline_cache[code] = fetch_daily_kline(code, days=kline_days.get(code, 30))
                df = kline_cache[code]
                if recompute_task(task, df):
                    success += 1
            except Exception:
                pass
            self.progress.emit(i + 1, total)
        backfill_triggered_watch_tasks(self._tasks, kline_cache)
        save_tasks_merge(self._tasks)
        self.completed.emit(success, total)


# =========================================================
# 追踪任务表格
# =========================================================
class _TrackingTable(QTableWidget):
    row_double_clicked = Signal(dict)

    _COLS = ['代码', '名称', '预测时间', '类型', '方向', '基准价', '审判日',
             '状态', '评级', '结论', '浮盈/最终%']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setStyleSheet(
            'QTableWidget { background:#080d1a; color:#ddd; gridline-color:#1a2a3a;'
            ' border:none; font-size:12px; alternate-background-color:#0d1525; }'
            'QTableWidget::item:selected { background:#1a3060; }'
            'QHeaderView::section { background:#0d1525; color:#8899bb;'
            ' border:none; border-bottom:1px solid #1a2a3a; padding:4px; }'
        )
        self.cellDoubleClicked.connect(self._on_double_clicked)

    def load(self, tasks: list[dict], cur_prices: dict):
        all_tasks = sorted(
            tasks,
            key=lambda t: t.get('created_at') or t.get('id') or '',
            reverse=True,
        )

        self.blockSignals(True)
        self.clear()
        self.setColumnCount(len(self._COLS))
        self.setHorizontalHeaderLabels(self._COLS)
        self.setRowCount(len(all_tasks))

        for row, task in enumerate(all_tasks):
            self._set_row(row, task, cur_prices)

        self.blockSignals(False)
        self.resizeColumnsToContents()
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def _set_row(self, row: int, task: dict, cur_prices: dict):
        is_open = task.get('status') == 'open'
        code     = task.get('code', '')
        kind     = task.get('kind', 'stock')
        entry    = float(task.get('entry_price', 0) or 0)
        grade    = task.get('grade') or ''
        deadline = task.get('deadline', '')
        direction = task.get('direction', '多')
        sample_label = '⚡触发买入' if task.get('converted_from_watch') else '虚拟观察'
        kind_str = ('🧩 板块' if kind == 'sector' else '📈 个股') + f'/{sample_label}'

        # 方向列优先显示 decision_view.bias_label
        dv = task.get('decision_view') or {}
        bias_label = dv.get('bias_label') or _DIR_LABEL.get(direction, direction)

        # 基准价列：买入验证是入场价，观察任务是分析时价。
        readiness = dv.get('readiness') or ''
        final_action = task.get('final_action', '')
        if entry <= 0:
            if final_action in ('watch_only', 'hold') and direction == 'bullish':
                entry_text = '未触发'
            elif direction in ('neutral',) or readiness in ('avoid',):
                entry_text = '—'
            else:
                entry_text = fmt_price(entry, code)
        else:
            entry_text = fmt_price(entry, code)

        converted = bool(task.get('converted_from_watch'))
        if is_open:
            cur_p = float((cur_prices.get(code) or {}).get('price') or entry)
            pnl_pct  = _pnl_pct(entry, cur_p, direction)
            pnl_str  = f'{pnl_pct:+.2f}%'
            status_str = f'触发买入验证  →{deadline}' if converted else f'进行中  →{deadline}'
            row_grade_color = ''
        else:
            d = task.get('grade_detail') or {}
            pnl_pct  = float(d.get('final_pct', 0))
            pnl_str  = f'{pnl_pct:+.2f}%'
            status_str = '已完成（触发买入）' if converted else '已完成'
            row_grade_color = _GRADE_COLOR.get(grade, '')

        pnl_color = '#2ecc71' if pnl_pct >= 0 else '#e74c3c'

        created_at = str(task.get('created_at', '') or '')
        created_text = created_at[5:16].replace('T', ' ') if created_at else ''
        verdict = _verdict_chip(task) or _conversion_wait_text(task)
        values = [
            code, task.get('name', ''), created_text, kind_str, bias_label,
            entry_text, deadline, status_str, grade, verdict, pnl_str,
        ]
        for col, val in enumerate(values):
            item = QTableWidgetItem(str(val))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if col == 8 and grade in _GRADE_COLOR:
                item.setForeground(QColor(_GRADE_COLOR[grade]))
            if col == 9 and verdict:
                item.setForeground(QColor(
                    '#2ecc71' if '✅' in verdict else ('#e74c3c' if '❌' in verdict else '#888')))
            if col == 10:
                item.setForeground(QColor(pnl_color))
            if col == 0:
                item.setData(Qt.ItemDataRole.UserRole, task)
            self.setItem(row, col, item)

    def update_floating_pnl(self, cur_prices: dict):
        pnl_col = 10
        for row in range(self.rowCount()):
            id_item = self.item(row, 0)
            if not id_item:
                continue
            task = id_item.data(Qt.ItemDataRole.UserRole)
            if not task or task.get('status') != 'open':
                continue
            code  = task.get('code', '')
            entry = float(task.get('entry_price', 0) or 0)
            direction = task.get('direction', '多')
            cur_p = float((cur_prices.get(code) or {}).get('price') or entry)
            pnl_pct = _pnl_pct(entry, cur_p, direction)
            pnl_item = self.item(row, pnl_col) or QTableWidgetItem()
            pnl_item.setText(f'{pnl_pct:+.2f}%')
            pnl_item.setForeground(QColor('#2ecc71' if pnl_pct >= 0 else '#e74c3c'))
            pnl_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setItem(row, pnl_col, pnl_item)

    def set_multi_delete_mode(self, enabled: bool):
        mode = (
            QAbstractItemView.SelectionMode.MultiSelection
            if enabled else QAbstractItemView.SelectionMode.SingleSelection
        )
        self.setSelectionMode(mode)

    def selected_tasks(self) -> list[dict]:
        selection = self.selectionModel()
        rows = sorted({idx.row() for idx in selection.selectedRows()}) if selection else []
        tasks: list[dict] = []
        for row in rows:
            item = self.item(row, 0)
            if item is None:
                continue
            task = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(task, dict):
                tasks.append(task)
        return tasks

    def _on_double_clicked(self, row: int, _col: int):
        id_item = self.item(row, 0)
        if id_item:
            task = id_item.data(Qt.ItemDataRole.UserRole)
            if task:
                self.row_double_clicked.emit(task)


# =========================================================
# 错误类型分类（AI 反思 [错误类型: X] 的含义，全局公开）
# =========================================================
ERROR_TYPE_DESC = {
    'A': '模型可改进（判断/选股/参数本可做得更好）',
    'B': '数据问题（输入数据缺失或失真，归因受限）',
    'C': '黑天鹅/外部冲击（不可预见的市场/政策事件）',
    'D': '方向对但交易执行错（择时/仓位/止盈止损操作问题）',
    'N': '无明显错误（决策与执行均合理）',
}


def error_type_label(code: str) -> str:
    """把单字母错误类型转成『X = 中文含义』。未知则原样返回。"""
    code = (code or '').strip().upper()
    desc = ERROR_TYPE_DESC.get(code)
    return f'{code} = {desc}' if desc else (code or '未判定')


# =========================================================
# 评级说明弹窗
# =========================================================
class SpecDialog(QDialog):
    _HTML = """
<h2 style="color:#e94560;">评级体系（AI 契约四分类）</h2>
<p style="color:#9ab;">AI 在最终决策层输出四类追踪口径，跟踪严格按该口径消费，全链路超短线 <b>2~5 个交易日</b>。A 股散户<b>不能做空个股</b>，跟踪只检验「预测方向」判断准不准。</p>

<p style="margin-top:12px;"><b>① 买入验证—— 触发优先（散户真能做的买入）</b></p>
<table border="1" cellpadding="6" style="border-color:#3a4a6a;color:#ddd;">
<tr><th>评级</th><th>含义</th></tr>
<tr><td style="color:#2ecc71;"><b>A 止盈</b></td><td>追踪期内先触及目标价</td></tr>
<tr><td style="color:#1abc9c;"><b>B 到期半程</b></td><td>未触发，到期收盘 ≥ 目标 50%</td></tr>
<tr><td style="color:#f1c40f;"><b>C 到期小赚</b></td><td>未触发，到期收盘 0 ~ 目标 50%</td></tr>
<tr><td style="color:#e67e22;"><b>D 到期小亏</b></td><td>未止盈，到期收盘亏损但未跌破止损线</td></tr>
<tr><td style="color:#e74c3c;"><b>F 止损</b></td><td>审判日收盘跌破止损线（个股 10% / ETF 5%）；过程中跌破不算</td></tr>
</table>
<p style="color:#9ab;margin-top:4px;">进行中买入单盘中触及目标价即提前止盈（标 A）；审判日再按持有期最高价复核最终涨幅。</p>

<p style="margin-top:12px;"><b>② 回避观察—— 跌幅兑现度（与买入口径对称）</b></p>
<p style="color:#9ab;">AI 给出预测跌幅与失效价。站在「你没买」用真实涨跌评。盘中扫描 2~5 天：先跌达标优先给 A；未先达标且收盘确认涨破失效价才给 F；全程未破但曾跌达目标即 A：</p>
<table border="1" cellpadding="6" style="border-color:#3a4a6a;color:#ddd;">
<tr><th>评级</th><th>含义</th></tr>
<tr><td style="color:#2ecc71;"><b>A 跌幅达标</b></td><td>实际跌幅 ≥ AI 预测跌幅</td></tr>
<tr><td style="color:#1abc9c;"><b>B 跌幅过半</b></td><td>实际跌幅 ≥ 预测跌幅 50%</td></tr>
<tr><td style="color:#f1c40f;"><b>C 横盘</b></td><td>未跌够半程，涨幅 &lt; 2%</td></tr>
<tr><td style="color:#e67e22;"><b>D 小涨踏空</b></td><td>涨 ≥ 2% 但未破失效价</td></tr>
<tr><td style="color:#e74c3c;"><b>F 大涨踏空</b></td><td>未先跌达标，且收盘确认涨破失效价</td></tr>
</table>

<p style="margin-top:12px;"><b>③ 中性观望—— 散户不踏空即正确</b></p>
<p style="color:#9ab;">观望单只怕踏空，涨幅是唯一扣分项：</p>
<ul>
<li><span style="color:#2ecc71;">A 观望正确</span> —— 涨幅 ≤ +4%（下跌=躲过下跌，横盘/小涨=判断合理）</li>
<li><span style="color:#e67e22;">D 踏空</span> —— 上涨 +4% ~ +6%（该出手没出手）</li>
<li><span style="color:#e74c3c;">F 大幅踏空</span> —— 上涨 &gt; +6%（错过较大行情）</li>
</ul>

<p style="margin-top:12px;"><b>④ 看多观察候选—— 等触发，触发变身买入</b></p>
<p style="color:#9ab;">AI 给出触发价、失效价、触发后预计涨幅。生命周期两段：</p>
<ul>
<li><b>触发判定从创建日的下一交易日起</b>（预测当日的盘中行情发生在预测之前，不构成触发），最早 high ≥ 触发价<b>且当日放量确认</b>（成交量 ≥ 前 5 日均量 ×1.5）的交易日<b>原单原地变身为买入验证单</b>，类型列显示 <b>⚡触发买入</b>；碰触发价但缩量的假突破当日不变身、次日继续扫，不永久作废候选。</li>
<li>成交价按 max(触发价, 当日 open) 保守记录；目标 = AI 触发后预计涨幅换算到实际成本；止损 = 失效价；<b>审判日沿用原单</b>（不延长）。变身后按买入单 A~F 评分（F 止损只在变身后产生）。</li>
<li>触发当日<b>收盘即跌破失效价</b> → 仍触发，但当日就按止损保守结算 F（不等审判日）。</li>
<li>触发日呈<b>一字板</b>（开 = 高 = 低且已在触发价上，疑似根本买不进）→ 标「疑似不可成交」，<b>不计入主看多兑现率</b>、在 KPI 单列。</li>
<li><b>未触发到期</b>按最终涨跌评 B/C/D：最终涨幅 ≥ 0 = <span style="color:#1abc9c;">B 观望合理</span>；-6% &lt; 最终涨幅 &lt; 0 = <span style="color:#f1c40f;">C 小跌观望合理</span>；最终涨幅 ≤ -6% = <span style="color:#e67e22;">D 分析有误但未买</span>。未变身看多观察不再给 A；触价但缩量会在结论列标明等待放量确认。</li>
<li>候选转换率 = 已触发（已变身）候选 / 全部看多观察候选（含仍在等触发的）。</li>
</ul>

<p style="margin-top:12px;"><b>路径质量：</b></p>
<ul>
<li>买入单：路径干净（回撤 &lt; 50% 止损线）/ 逼近（50%~90%）/ 承压（≥ 90%）</li>
<li>回避 / 观望 / 候选：观望路径（无持仓，不按止损线评）</li>
</ul>
<p style="margin-top:6px;color:#9ab;">有效率口径：<b>有效 = A/B/C</b>；<b>强兑现/强回避 = A/B</b>；<b>失效 = D/F</b>。各类<b>各自单独</b>统计。统计基准从触发日（变身单）或创建日起算同期大盘。</p>

<p style="margin-top:12px;"><b>板块 αβγδ 四级评分：</b></p>
<ul>
<li>α 优 —— 最终涨跌幅 ≥ 目标涨幅</li>
<li>β 良 —— 目标涨幅 × 0.5 ≤ 最终涨跌幅 &lt; 目标涨幅</li>
<li>γ 平 —— -止损幅度 ≤ 最终涨跌幅 &lt; 目标涨幅 × 0.5</li>
<li>δ 差 —— 最终涨跌幅 &lt; -止损幅度</li>
</ul>

<p style="margin-top:12px;"><b>方向有效率（达最低样本量才显示：个股每方向 ≥ 5、板块 ≥ 3）：</b></p>
<ul>
<li>看多兑现率 = (A+B+C) / 已评级看多单</li>
<li>回避有效率 = (A+B+C) / 已评级看跌回避单</li>
<li>观望合理率 = (A+B+C) / 已评级中性观望单</li>
<li>板块胜率 = (α + β) / 已结算板块数</li>
<li>候选转换率 = 已触发（已变身）候选数 / 全部看多观察候选数（含仍在等触发的 open 候选）。</li>
<li>主看多兑现率会排除 fill_suspect=True 的疑似不可成交样本，这类样本在 KPI 中单列显示。</li>
</ul>
<p style="margin-top:12px;"><b>系统判断力（第三行）—— 一句话：你看多的票，是不是真的比你回避的票更扛涨？</b></p>
<p>普跌行情里，看多绝对胜率一定难看（啥都在跌）；但只要「看多的票」比「回避的票」扛跌，就说明系统把强弱排对了边。这个数以中证全指为大盘基准、按每只票各自持有区间剔除同期大盘涨跌，看的是「纯本事」（超额 alpha）。</p>
<ul>
<li><b>系统判断力 = 看多组平均超额 − 回避组平均超额</b>（各自已剔除同期中证全指，也叫排序价差）。&gt; 0 = 分对边（绿）、&lt; 0 = 排反了（红），数越大越会分。<br>
示例：看多组超额 +0.3%、回避组超额 -1.8%，相差 <b>+2.1%</b> → 看多的票确实比回避的更扛跌，系统排对了。</li>
<li><b>相对基准</b>：每组相对中证全指的超额（基准 = 中证全指同期涨跌，按每只票各自持有区间对齐）。看多越正越好；<b>回避这格越负越好</b>（比大盘跌得多 = 躲对了），所以这三个数只作参考、不标红绿。</li>
<li><b>同一只票算几笔</b>：同方向的多次分析合成一笔；<b>方向变了就算新的一笔</b>（先看跌后看涨，会分别进回避组和看多组，都算数）。</li>
<li>每组至少 5 只票才显示，不够就标「积累中」。</li>
</ul>
<h2 style="color:#e94560;margin-top:16px;">AI 反思错误类型</h2>
<p>每条 AI 反思开头会标注 <b>[错误类型: X]</b>，含义如下：</p>
<table border="1" cellpadding="6" style="border-color:#3a4a6a;color:#ddd;">
<tr><th>类型</th><th>含义</th></tr>
<tr><td style="color:#f1c40f;"><b>A</b></td><td>模型可改进 —— 判断/选股/参数本可做得更好</td></tr>
<tr><td style="color:#3498db;"><b>B</b></td><td>数据问题 —— 输入数据缺失或失真，归因受限</td></tr>
<tr><td style="color:#9b59b6;"><b>C</b></td><td>黑天鹅/外部冲击 —— 不可预见的市场/政策事件</td></tr>
<tr><td style="color:#e67e22;"><b>D</b></td><td>方向对但交易执行错 —— 择时/仓位/止盈止损操作问题</td></tr>
<tr><td style="color:#2ecc71;"><b>N</b></td><td>无明显错误 —— 决策与执行均合理</td></tr>
</table>
"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('评级说明')
        self.resize(640, 580)
        self.setStyleSheet('background:#0d0d1f; color:#e0e0e0;')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        box = QTextBrowser()
        box.setHtml(self._HTML)
        box.setStyleSheet(
            'QTextBrowser { background:#080d1a; color:#e0e0e0;'
            ' border:1px solid #1a2a3a; padding:8px; font-size:14px; }'
        )
        lay.addWidget(box, stretch=1)
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet(
            'QPushButton { background:#1a2a40; color:#e0e0e0; border:none;'
            ' padding:8px 22px; border-radius:4px; }'
            'QPushButton:hover { background:#2a3a5a; }'
        )
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        lay.addLayout(row)


# =========================================================
# 任务详情弹窗（3 子 Tab：行情 / 分析 / 反思评分）
# =========================================================
class TrackingDetailDialog(QDialog):
    intel_anchor_clicked = Signal(str)  # event_id
    regenerate_retro_requested = Signal(dict)  # task
    task_link_requested = Signal(str)  # task_id

    def __init__(self, task: dict, cur_price: float | None, parent=None):
        super().__init__(parent)
        self._task = task
        self._cur_price = cur_price
        name = task.get('name', '')
        code = task.get('code', '')
        self.setWindowTitle(f'追踪详情 — {name} ({code})')
        self.resize(1200, 820)
        self.setMinimumSize(900, 600)
        self.setStyleSheet('background:#0d0d1f; color:#e0e0e0;')
        self._build_ui()

    def _on_task_anchor(self, url):
        text = url.toString()
        if text.startswith('tracking-task:'):
            self.task_link_requested.emit(text.split(':', 1)[1])

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # 顶部 Strip：评级 + 入场 + 当前/结算价 + 倒计时
        strip = QFrame()
        strip.setFixedHeight(64)
        strip.setStyleSheet('background:#0d1525; border:1px solid #1a2a3a; border-radius:4px;')
        s_lay = QHBoxLayout(strip)
        s_lay.setContentsMargins(16, 8, 16, 8)
        self._strip_lbl = QLabel(self._fmt_strip())
        self._strip_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._strip_lbl.setStyleSheet('font-size:14px;')
        s_lay.addWidget(self._strip_lbl)
        s_lay.addStretch()
        lay.addWidget(strip)

        # Tab Bar
        tabs = QTabWidget()
        tabs.setStyleSheet(
            'QTabWidget::pane { border:1px solid #1a2a3a; background:#080d1a; }'
            'QTabBar::tab { background:#0d1525; color:#888; padding:8px 22px;'
            ' border:1px solid #1a2a3a; border-bottom:none; font-size:13px; }'
            'QTabBar::tab:selected { background:#1a2a40; color:#e0e0e0; }'
        )
        tabs.addTab(self._build_quote_tab(), '📊 行情')
        tabs.addTab(self._build_analysis_tab(), '🧠 分析')
        tabs.addTab(self._build_review_tab(), '🤔 反思/评分')
        lay.addWidget(tabs, stretch=1)

        # 底部操作按钮
        row = QHBoxLayout()
        row.addStretch()
        self._copy_btn = QPushButton('📋 复制全文')
        self._copy_btn.clicked.connect(self._on_copy_full_text)
        self._copy_btn.setStyleSheet(
            'QPushButton { background:#17304a; color:#9ed0ff; border:none;'
            ' padding:6px 14px; border-radius:4px; }'
            'QPushButton:hover { background:#244a70; color:#ffffff; }'
            'QPushButton:disabled { background:#101824; color:#667788; }'
        )
        row.addWidget(self._copy_btn)
        close_btn = QPushButton('关闭')
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet(
            'QPushButton { background:#1a2a40; color:#e0e0e0; border:none;'
            ' padding:6px 18px; border-radius:4px; }'
            'QPushButton:hover { background:#2a3a5a; }'
        )
        row.addWidget(close_btn)
        lay.addLayout(row)

    def _fmt_strip(self) -> str:
        task = self._task
        name = task.get('name', '')
        code = task.get('code', '')
        entry = float(task.get('entry_price', 0) or 0)
        direction = task.get('direction', '多')
        status = task.get('status', 'open')
        grade  = task.get('grade') or ''
        deadline = task.get('deadline', '')
        dv = task.get('decision_view') or {}
        bias_label = dv.get('bias_label') or _DIR_LABEL.get(direction, direction)
        head_dir = dv.get('fusion_label') or bias_label
        entry_display = fmt_price(entry, code) if entry > 0 else '未触发'
        price_label = tracking_price_label(task)

        head = (f'<b style="color:#ddd;font-size:13px;">{name}</b>'
                f'<span style="color:#666;"> ({code})</span>  '
                f'<span style="color:#888;">方向: {head_dir}  {price_label}: {entry_display}</span>  ')

        if status == 'open':
            cur = self._cur_price or entry
            pnl_pct = _pnl_pct(entry, cur, direction)
            pnl_color = '#2ecc71' if pnl_pct >= 0 else '#e74c3c'
            try:
                days_left = (date.fromisoformat(deadline) - date.today()).days
                ddl_html = (f'<span style="color:#f0b429;">⏳ 还剩 {days_left} 天</span>'
                            if days_left >= 0 else
                            f'<span style="color:#e74c3c;">⚠️ 已过期</span>')
            except Exception:
                ddl_html = ''
            wait_text = _conversion_wait_text(task, detailed=True)
            wait_html = f'  <span style="color:#f0b429;">{wait_text}</span>' if wait_text else ''
            return (head +
                    f'<span style="color:#aaa;">当前: {fmt_price(cur, code)}</span>  '
                    f'<span style="color:{pnl_color};font-weight:bold;">{pnl_pct:+.2f}%</span>  '
                    + ddl_html + wait_html)
        else:
            d = task.get('grade_detail') or {}
            final_pct = float(d.get('final_pct', 0))
            deadline_price = float(d.get('deadline_price', 0))
            pnl_color = '#2ecc71' if final_pct >= 0 else '#e74c3c'
            grade_color = _GRADE_COLOR.get(grade, '#888')
            closed_at = (task.get('closed_at') or '')[:10]
            return (head +
                    f'<span style="color:#aaa;">结算价: {fmt_price(deadline_price, code)}</span>  '
                    f'<span style="color:{grade_color};font-size:15px;font-weight:bold;">评级 {grade}</span>  '
                    f'<span style="color:{pnl_color};font-weight:bold;">{final_pct:+.2f}%</span>  '
                    f'<span style="color:#666;">结算 {closed_at}</span>')

    def _build_quote_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        task = self._task
        entry = float(task.get('entry_price', 0) or 0)
        code = task.get('code', '')
        kline = task.get('kline_at_close') or []
        created_fmt = (task.get('created_at', '') or '')[:16].replace('T', ' ')
        deadline_fmt = task.get('deadline', '')
        dv = task.get('decision_view') or {}
        bias_label = dv.get('bias_label') or ''
        fusion_label = dv.get('fusion_label') or ''
        action_label = dv.get('action_label') or ''
        eval_scope = dv.get('evaluation_scope') or ''
        entry_text = fmt_price(entry, code) if entry > 0 else ('未触发' if task.get('final_action') in ('watch_only', 'hold') and task.get('direction') == 'bullish' else '—')
        price_label = tracking_price_label(task)
        text_lines = [
            f'追踪期间: {created_fmt} → {deadline_fmt}',
            f'{price_label}: {entry_text}',
            f'样本类型: {sample_type_label(task.get("sample_type"))}',
            f'策略标签: {task.get("strategy_tag") or "未分类"}',
        ]
        if fusion_label:
            text_lines.append(fusion_label)
        elif bias_label:
            text_lines.append(f'方向倾向: {bias_label} / 操作动作: {action_label}')
        if eval_scope and eval_scope != 'trade':
            scope_label = {
                'avoid_watch': '回避观察',
                'neutral_watch': '中性观察',
                'direction_watch': '方向观察',
            }.get(eval_scope, '方向观察' if 'watch' in eval_scope else eval_scope)
            text_lines.append(f'验证口径: {scope_label}（仅作观察统计）')
        risk_flags = task.get('risk_flags') or []
        if risk_flags:
            text_lines.append(f'风险标记: {" / ".join(str(x) for x in risk_flags)}')
        related_html = self._related_chain_html(task)
        if task.get('status') == 'open':
            cur = self._cur_price or entry
            pnl_pct = _pnl_pct(entry, cur, task.get('direction', '多'))
            text_lines.append(f'当前价: {fmt_price(cur, code)}（实时）')
            text_lines.append(f'盈亏: {pnl_pct:+.2f}%')
        elif kline:
            highs = [r['h'] for r in kline]
            lows  = [r['l'] for r in kline]
            closes = [r['c'] for r in kline]
            d = task.get('grade_detail') or {}
            text_lines.extend([
                f'结算价: {fmt_price(d.get("deadline_price", 0), code)}',
                f'期间最高: {fmt_price(max(highs), code)}  最低: {fmt_price(min(lows), code)}',
                f'结束 OHLC ({len(kline)} 根 K 线):',
                f'  开 {fmt_price(kline[-1]["o"], code)}  高 {fmt_price(kline[-1]["h"], code)}  '
                f'低 {fmt_price(kline[-1]["l"], code)}  收 {fmt_price(kline[-1]["c"], code)}',
            ])
        else:
            text_lines.append('（无 K 线快照）')

        box = QTextBrowser()
        if related_html:
            body = '<br>'.join(str(x) for x in text_lines)
            box.setHtml(related_html + '<hr style="border:0;border-top:1px solid #1a2a3a;">' + body)
            box.setOpenExternalLinks(False)
            box.anchorClicked.connect(self._on_task_anchor)
        else:
            box.setPlainText('\n'.join(text_lines))
        box.setStyleSheet('QTextBrowser { background:#080d1a; color:#e0e0e0;'
                          ' border:1px solid #1a2a3a; border-radius:4px;'
                          ' font-size:13px; padding:12px; line-height:1.6; }')
        lay.addWidget(box, stretch=1)
        return w

    def _related_chain_html(self, task: dict) -> str:
        from_id = str(task.get('converted_from_task_id') or '')
        to_id = str(task.get('converted_to_task_id') or '')
        if not from_id and not to_id:
            return ''
        parts = ['<div style="color:#c8d8f8;font-size:13px;">关联链：']
        if from_id:
            parts.append(f'<a style="color:#6fb1ff;" href="tracking-task:{from_id}">原候选 {from_id}</a>')
            parts.append(' → 当前买入验证')
        else:
            parts.append('当前候选 → ')
            parts.append(f'<a style="color:#6fb1ff;" href="tracking-task:{to_id}">买入验证 {to_id}</a>')
        parts.append('</div>')
        return ''.join(parts)

    @staticmethod
    def _fmt_agent(d) -> str:
        from core.agents.base import agent_dict_to_text
        if isinstance(d, dict):
            return agent_dict_to_text(d)
        return str(d) if d else ''

    def _build_analysis_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(12, 12, 12, 12)
        c_lay.setSpacing(8)

        task = self._task
        pred = task.get('pred_snapshot') or {}
        _RATING_LABEL = {
            'strong_buy': '强烈买入 ★★', 'buy': '买入 ★',
            'hold': '观望 —', 'sell': '卖出 ▼', 'strong_sell': '强烈卖出 ▼▼',
        }
        final_rating = pred.get('final_rating', task.get('final_rating', 'hold')) or 'hold'
        thesis = pred.get('thesis') or pred.get('reasoning') or ''
        g_text = f"综合评级：{_RATING_LABEL.get(final_rating, final_rating)}\n\n{thesis}" if thesis \
            else f"综合评级：{_RATING_LABEL.get(final_rating, final_rating)}"
        from core.agents.base import agent_dict_to_html
        ref_ids = pred.get('_intel_ref_ids') or {}
        # 纯文本 sections
        plain_sections = [
            ('📋 预测依据', self._fmt_pred_summary()),
            ('⚔️ F 层多空辩论', str(pred.get('_debate', '') or '')),
            ('🎯 G 层最终决策', g_text),
            ('📰 情报快照', self._fmt_intel_snapshot()),
        ]
        for title, content in plain_sections:
            if not content or not content.strip():
                content = '（无内容）'
            c_lay.addWidget(self._make_section(title, content))
        # Agent sections 用 HTML（intel_refs 可点击）
        agent_sections = [
            ('📡 宏观/情报 Agent', pred.get('_agent_macro')),
            ('🏢 基本面 Agent', pred.get('_agent_company')),
            ('📈 技术面 Agent', pred.get('_agent_technical')),
            ('💰 资金流向 Agent', pred.get('_agent_fundflow')),
            ('📰 新闻/事件 Agent', pred.get('_agent_news')),
        ]
        for title, raw in agent_sections:
            d = raw
            if isinstance(d, str) and d.strip().startswith('{'):
                try:
                    import json as _j
                    p = _j.loads(d)
                    if isinstance(p, dict) and 'stance' in p:
                        d = p
                except Exception:
                    pass
            if isinstance(d, dict):
                html_content = agent_dict_to_html(d, ref_ids) or '（无内容）'
                c_lay.addWidget(self._make_section(title, html_content, html=True))
            else:
                txt = str(d) if d else '（无内容）'
                c_lay.addWidget(self._make_section(title, txt))
        c_lay.addStretch()
        scroll.setWidget(container)
        return scroll

    def _build_review_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        task = self._task

        if task.get('status') == 'open':
            ddl = task.get('deadline', '')
            tip = QLabel(f'⏳ 审判日 {ddl} 后评分')
            tip.setStyleSheet('color:#f0b429; font-size:14px; padding:12px 24px 4px 24px;')
            tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(tip)
            criteria = QTextBrowser()
            criteria.setOpenLinks(False)
            criteria.setStyleSheet(
                'QTextBrowser { background:#080d1a; color:#8899aa;'
                ' border:1px solid #1a2a3a; border-radius:4px;'
                ' font-size:12px; padding:10px; }'
            )
            criteria.setHtml(format_open_review_rule_html(task))
            criteria.setFixedHeight(200)
            lay.addWidget(criteria)
            lay.addStretch()
            return w

        # 已完成：评分卡 + AI 反思
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(0, 0, 0, 0)
        c_lay.setSpacing(8)

        c_lay.addWidget(self._make_section('📊 评分详情', self._fmt_grade_detail()))

        # AI 反思错误类型说明（公开含义，避免只显示一个字母 B）
        err_type = task.get('error_type')
        self._err_type_lbl = QLabel()
        self._err_type_lbl.setWordWrap(True)
        self._err_type_lbl.setStyleSheet(
            'color:#aac8e8; font-size:12px; background:#10203a;'
            ' border:1px solid #24384a; border-radius:4px; padding:6px 10px;'
        )
        if err_type:
            self._err_type_lbl.setText(f'本次 AI 判定 · 错误类型 {error_type_label(err_type)}')
        else:
            self._err_type_lbl.hide()
        c_lay.addWidget(self._err_type_lbl)

        retro = task.get('retrospective') or '（暂无 AI 反思，点击下方按钮生成）'
        retro_widget = self._make_section('🤔 AI 反思', retro, max_height=1200)
        self._retro_browser = retro_widget.findChild(QTextBrowser)
        c_lay.addWidget(retro_widget)

        self._retro_btn = QPushButton(
            '🔄 重新生成反思' if task.get('retrospective') else '✨ 生成 AI 反思'
        )
        self._retro_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retro_btn.setStyleSheet(
            'QPushButton { background:#1a2a3a; color:#c8d8f8; border:1px solid #2a3a4a;'
            ' border-radius:4px; padding:6px 16px; font-size:13px; }'
            ' QPushButton:hover { background:#24384a; }'
            ' QPushButton:disabled { color:#667788; background:#101824; }'
        )
        self._retro_btn.clicked.connect(self._on_regenerate_clicked)
        c_lay.addWidget(self._retro_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        c_lay.addStretch()
        scroll.setWidget(container)
        lay.addWidget(scroll, stretch=1)
        return w

    def _make_section(self, title: str, text: str, html: bool = False,
                      max_height: int = 480) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet('background:#0d1525; border:1px solid #1a2a3a; border-radius:4px;')
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(12, 8, 12, 8)
        lbl = QLabel(title)
        lbl.setStyleSheet('color:#c8d8f8; font-size:13px; font-weight:bold;')
        wl.addWidget(lbl)
        box = QTextBrowser()
        box.setStyleSheet('QTextBrowser { background:#080d1a; color:#e0e0e0;'
                          ' border:none; font-size:13px; padding:6px;'
                          ' line-height:1.5; }')
        if html:
            box.setOpenLinks(False)
            box.setHtml(
                f'<div style="color:#e0e0e0;font-size:13px;'
                f'font-family:Microsoft YaHei;line-height:1.6;">{text}</div>'
            )
            box.anchorClicked.connect(
                lambda url: self.intel_anchor_clicked.emit(url.toString()[6:])
                if url.toString().startswith('intel_') else None
            )
            line_count = max(text.count('<br>') + 1, 4)
        else:
            box.setPlainText(text)
            line_count = max(text.count('\n') + 1, 4)
        box.setMinimumHeight(min(120 + line_count * 18, max_height))
        wl.addWidget(box)
        return wrap

    def _on_copy_full_text(self) -> None:
        QApplication.clipboard().setText(self._build_copy_text())
        btn = getattr(self, '_copy_btn', None)
        if btn is not None:
            btn.setEnabled(False)
            btn.setText('已复制')
            QTimer.singleShot(1200, self._restore_copy_button)

    def _restore_copy_button(self) -> None:
        btn = getattr(self, '_copy_btn', None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText('📋 复制全文')

    @staticmethod
    def _copy_section(title: str, content: str) -> str:
        text = str(content or '').strip() or '（无内容）'
        return f'【{title}】\n{text}'

    def _build_copy_text(self) -> str:
        task = self._task
        pred = task.get('pred_snapshot') or {}
        name = task.get('name', '')
        code = task.get('code', '')
        created_fmt = (task.get('created_at', '') or '')[:16].replace('T', ' ')
        deadline_fmt = task.get('deadline', '')
        entry = float(task.get('entry_price', 0) or 0)
        price_label = tracking_price_label(task)
        entry_text = fmt_price(entry, code) if entry > 0 else '未触发'
        grade = task.get('grade') or '未评级'
        status = '进行中' if task.get('status') == 'open' else '已结算'

        _rating_label = {
            'strong_buy': '强烈买入 ★★', 'buy': '买入 ★',
            'hold': '观望 —', 'sell': '卖出 ▼', 'strong_sell': '强烈卖出 ▼▼',
        }
        final_rating = pred.get('final_rating', task.get('final_rating', 'hold')) or 'hold'
        thesis = pred.get('thesis') or pred.get('reasoning') or ''
        g_text = (
            f'综合评级：{_rating_label.get(final_rating, final_rating)}\n\n{thesis}'
            if thesis else f'综合评级：{_rating_label.get(final_rating, final_rating)}'
        )

        sections: list[str] = [
            f'{name}（{code}）追踪详情',
            f'复制时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            '',
            self._copy_section('基本信息', '\n'.join([
                f'状态：{status}',
                f'预测时间：{created_fmt or "—"}',
                f'审判日：{deadline_fmt or "—"}',
                f'{price_label}：{entry_text}',
                f'评级：{grade}',
                f'样本类型：{sample_type_label(task.get("sample_type"))}',
                f'策略标签：{task.get("strategy_tag") or "未分类"}',
            ])),
            self._copy_section('AI分析 - 预测依据', self._fmt_pred_summary(limit=None)),
            self._copy_section('AI分析 - F层多空辩论', str(pred.get('_debate', '') or '')),
            self._copy_section('AI分析 - G层最终决策', g_text),
            self._copy_section('AI分析 - 情报快照', self._fmt_intel_snapshot()),
        ]

        for title, raw in [
            ('宏观/情报 Agent', pred.get('_agent_macro')),
            ('基本面 Agent', pred.get('_agent_company')),
            ('技术面 Agent', pred.get('_agent_technical')),
            ('资金流向 Agent', pred.get('_agent_fundflow')),
            ('新闻/事件 Agent', pred.get('_agent_news')),
        ]:
            data = raw
            if isinstance(data, str) and data.strip().startswith('{'):
                try:
                    import json as _json
                    parsed = _json.loads(data)
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    pass
            sections.append(self._copy_section(f'AI分析 - {title}', self._fmt_agent(data)))

        if task.get('status') == 'open':
            rating_text = f'任务尚未到审判日，当前不生成最终评级；到 {deadline_fmt or "审判日"} 后按下方标准自动评分。'
        else:
            rating_text = self._fmt_grade_detail()
        sections.append(self._copy_section('评级分析', rating_text))
        sections.append(self._copy_section('AI反思', task.get('retrospective') or '（暂无 AI 反思）'))
        sections.append(self._copy_section('审判日标准', format_open_review_rule_text(task)))
        return '\n\n'.join(sections)

    def _on_regenerate_clicked(self) -> None:
        if getattr(self, '_retro_btn', None) is not None:
            self._retro_btn.setEnabled(False)
            self._retro_btn.setText('⏳ 生成中…')
        if getattr(self, '_retro_browser', None) is not None:
            self._retro_browser.setPlainText('（AI 反思生成中，请稍候…）')
        self.regenerate_retro_requested.emit(self._task)

    def update_retrospective(self, text: str) -> None:
        """AI 反思完成后更新弹窗内的反思文本（任务仍开着时调用）。"""
        self._task['retrospective'] = text
        if getattr(self, '_retro_browser', None) is not None:
            self._retro_browser.setPlainText(text)
        import re as _re
        m = _re.search(r'\[错误类型:\s*([ABCDN])\]', text[:300])
        et = m.group(1) if m else self._task.get('error_type')
        lbl = getattr(self, '_err_type_lbl', None)
        if lbl is not None and et:
            lbl.setText(f'本次 AI 判定 · 错误类型 {error_type_label(et)}')
            lbl.show()
        if getattr(self, '_retro_btn', None) is not None:
            self._retro_btn.setEnabled(True)
            self._retro_btn.setText('🔄 重新生成反思')

    def _fmt_pred_summary(self, limit: int | None = 800) -> str:
        task = self._task
        conf = float(task.get('confidence', 0))
        reasoning = str(task.get('reasoning', '') or '')
        if limit is not None:
            reasoning = reasoning[:limit]
        dv = task.get('decision_view') or {}
        bias_label = dv.get('bias_label') or ''
        fusion_label = dv.get('fusion_label') or ''
        action_label = dv.get('action_label') or ''
        direction_label = bias_label or _DIR_LABEL.get(str(task.get('direction') or ''), task.get('direction') or '—')
        lines = [
            f'方向: {direction_label}  置信度: {conf:.0f}/10',
            format_prediction_plan_line(task),
        ]
        if fusion_label:
            lines.append(fusion_label)
        elif bias_label:
            lines.append(f'方向倾向: {bias_label} | 操作动作: {action_label}')
        lines.extend([
            '',
            reasoning or '（无预测摘要）',
        ])
        return '\n'.join(lines)

    def _fmt_intel_snapshot(self) -> str:
        intel = self._task.get('intel_snapshot') or []
        if not intel:
            return '（无）'
        return '\n'.join(
            f"[{e.get('level','?')}] {e.get('date','?')}  {e.get('title','')}"
            for e in intel[:20]
        )

    def _fmt_grade_detail(self) -> str:
        task = self._task
        d = task.get('grade_detail') or {}
        tier_desc = {
            'E1': '旧版结果层 E1：达标（≥目标幅度）', 'E2': '旧版结果层 E2：半程（≥目标*50%）',
            'E3': '旧版结果层 E3：持平偏正（0% ~ 目标*50%）', 'E4': '旧版结果层 E4：小亏（未穿止损线）',
            'E5': '旧版结果层 E5：轻微穿止损', 'E6': '旧版结果层 E6：重度穿止损',
            'N_range_hit': '中性观察命中预设区间',
            'N_slight_miss': '中性观察轻微偏离预设区间',
            'N_major_miss': '中性观察明显偏离预设区间',
            'N_no_range': '无预设观察区间（按±5%/±8%观察带评分）',
            'T_target': '止盈达标（触及目标价）',
            'T_stop': '止损出局（审判日跌破止损线）',
            'T_expire_half': '到期半程（≥目标*50%）',
            'T_expire_gain': '到期小赚（0 ~ 目标*50%）',
            'T_expire_loss': '到期小亏（未破止损）',
            'fall_big': '大幅下跌（≤-5%）',
            'fall_small': '小幅下跌（-5% ~ -2%）',
            'flat': '基本横盘（±2%）',
            'rise_small': '小幅上涨（+2% ~ +5%）',
            'rise_big': '大幅上涨（≥+5%）',
            'in_range': '落在预期区间内',
            'below': '跌出预期区间下沿',
            'below_far': '大幅跌破预期区间',
            'above': '涨出预期区间上沿',
            'above_far': '大幅涨破预期区间',
            'bull_big_gain': '大幅上涨（≥+5%）',
            'bull_gain': '上涨（+2% ~ +5%）',
            'bull_flat': '基本横盘（±2%）',
            'bull_small_loss': '下跌（-2% ~ -5%）',
            'bull_big_loss': '大幅下跌（≤-5%，看多落空）',
            'watch_dodge': '下跌躲过（观望正确）',
            'watch_flat': '未上涨 / 横盘（观望正确）',
            'watch_miss': '踏空上涨（+4% ~ +6%）',
            'watch_miss_big': '大幅踏空（>+6%）',
        }
        path_desc = {
            'P_clean': '路径干净（回撤 <50% 止损线）',
            'P_tested': '路径逼近（回撤 50%~90% 止损线）',
            'P_stressed': '路径承压（回撤 ≥90% 止损线）',
            'neutral': '观望路径（中性 / 观望任务）',
        }
        tier = d.get('outcome_tier', '')
        path = d.get('path_quality', '')
        grade = task.get('grade', '')
        entry = float(d.get('entry_price', 0) or 0)
        peak_pct = float(d.get('peak_pct', 0) or 0)
        trough_pct = float(d.get('trough_pct', 0) or 0)
        if entry > 0 and ('peak_pct' not in d or 'trough_pct' not in d):
            kline = task.get('kline_at_close') or []
            try:
                highs = [float(r.get('h', 0) or 0) for r in kline]
                lows = [float(r.get('l', 0) or 0) for r in kline]
                if highs and lows:
                    peak_pct = (max(highs) - entry) / entry * 100
                    trough_pct = (min(lows) - entry) / entry * 100
            except Exception:
                pass
        task_class = d.get('task_class') or task.get('task_class') or ''
        if not task_class:
            _dir = str(d.get('direction') or task.get('direction') or '')
            task_class = 'avoid' if _dir == 'bearish' else ('buy' if _dir == 'bullish' else 'watch')
        price_label = tracking_price_label(task, d)
        if task_class == 'avoid':
            if tier == 'drop_target':
                verdict = '✅ 盘中跌幅达标（目标优先）'
            elif tier == 'drop_half':
                verdict = '✅ 回避有效（跌幅半程）'
            elif grade in ('A', 'B'):
                verdict = '✅ 回避正确（避开下跌）'
            elif grade == 'C':
                verdict = '➖ 回避中性（基本横盘）'
            else:
                verdict = '❌ 回避失败（踏空上涨）'
            target_verdict = (
                '✅ 跌幅目标达标（目标优先）'
                if tier in ('drop_target', 'drop_half')
                else '不适用（回避单不按目标价达标评价）'
            )
        elif task_class == 'watch':
            fp = float(d.get('final_pct', 0) or 0)
            if fp > 4.0:
                verdict = '❌ 观望踏空（错过上涨）'
            elif fp <= -1.5:
                verdict = '✅ 观望正确（躲过下跌）'
            else:
                verdict = '✅ 观望正确（横盘合理）'
            target_verdict = '不适用（观望不按目标价达标评价）'
        else:
            verdict = '✅ 看多兑现（A/B/C）' if grade in ('A', 'B', 'C') else '❌ 看多落空（D/F）'
            target_verdict = '✅ 达标（A/B）' if grade in ('A', 'B') else '❌ 未达标'
        metric_lines = format_directional_metric_lines(
            {**d, 'peak_pct': peak_pct, 'trough_pct': trough_pct},
            task,
        )
        lines = [
            metric_lines[0],
            metric_lines[1],
            f'结算价: {fmt_price(d.get("deadline_price", 0), self._task.get("code"))}  {price_label}: {fmt_price(entry, self._task.get("code"))}',
            '',
            f'结果层级: {tier_desc.get(tier, tier or "未记录")}',
            f'路径质量: {path_desc.get(path, path or "未记录")}',
            '',
            f'胜败判定: {verdict}',
            f'达标判定: {target_verdict}',
        ]
        watch_quality = d.get('watch_quality') or d.get('execution_quality')
        if watch_quality:
            lines.append(f'观察评价: {watch_quality}')
        agent_lines = format_agent_hit_lines(d)
        if agent_lines:
            lines.extend(['', 'Agent 命中责任:', *agent_lines])
        return '\n'.join(lines)


# =========================================================
# 追踪面板（顶级 Tab）
# =========================================================
class TrackingPanel(QWidget):
    status_changed        = Signal(str, str)
    point_count_changed   = Signal(int)
    bottom_status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[dict] = []
        self._current_prices: dict = {}
        self._retro_workers: list[_RetrospectiveWorker] = []
        self._auto_retro_started: set[str] = set()
        self._auto_retro_queue: list[dict] = []
        self._retro_paused = False
        self._settlement_worker: _SettlementWorker | None = None
        self._settle_started = False
        self._settle_attempted: set[str] = set()
        self._convert_cooldown: dict[str, float] = {}
        self._initial_sweep_done = False
        self._refresh_btn: QPushButton | None = None
        self._refresh_in_progress = False
        self._refresh_is_manual = False
        self._last_auto_maintenance_ts = 0.0
        self._post_close_maintenance_date = ''
        self._recompute_worker: _RecomputeWorker | None = None
        self._word_export_worker: _WordExportWorker | None = None
        self._word_export_btn: QPushButton | None = None
        self._delete_multi_mode = False
        self._delete_btn: QPushButton | None = None
        self._filter_mode = 'all'   # all | open | closed
        self._search_text = ''
        self.alert_center = None
        self._open_detail_dlg: 'TrackingDetailDialog | None' = None
        self._build_ui()
        self._load_tasks()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # KPI 横条（3 行：核心指标 + 评级/路径 + 系统判断力）
        kpi_bar = QWidget()
        kpi_bar.setFixedHeight(170)
        kpi_bar.setStyleSheet(
            'background:qlineargradient(x1:0,y1:0,x2:0,y2:1,'
            ' stop:0 #0d1830, stop:1 #0a1020);'
            ' border-bottom:1px solid #1a2a3a;'
        )
        # 左侧两行统计文字占满宽度，右侧按钮排两列网格，互不遮挡
        kpi_outer = QHBoxLayout(kpi_bar)
        kpi_outer.setContentsMargins(16, 8, 16, 8)
        kpi_outer.setSpacing(16)

        # 左：核心 KPI（胜率/达标率/总数）+ 评级/路径 + 系统判断力
        stats_col = QVBoxLayout()
        stats_col.setSpacing(4)
        self._kpi_lbl = QLabel('—')
        self._kpi_lbl.setWordWrap(True)
        self._kpi_lbl.setStyleSheet(
            'color:#eef4ff; font-size:21px; font-weight:bold;'
        )
        stats_col.addWidget(self._kpi_lbl)
        self._kpi_lbl2 = QLabel('—')
        self._kpi_lbl2.setWordWrap(True)
        self._kpi_lbl2.setStyleSheet(
            'color:#aab8d8; font-size:16px;'
        )
        self._kpi_lbl2.setTextFormat(Qt.TextFormat.RichText)
        stats_col.addWidget(self._kpi_lbl2)
        self._kpi_lbl3 = QLabel('—')
        self._kpi_lbl3.setWordWrap(True)
        self._kpi_lbl3.setStyleSheet('font-size:17px;')
        self._kpi_lbl3.setTextFormat(Qt.TextFormat.RichText)
        stats_col.addWidget(self._kpi_lbl3)
        stats_col.addStretch()
        kpi_outer.addLayout(stats_col, stretch=1)

        # 右：操作按钮两列网格
        spec_btn = QPushButton('ℹ️ 评级说明')
        spec_btn.setStyleSheet(
            'QPushButton { background:#2a3a5a; color:#aac8e8; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 12px; }'
            'QPushButton:hover { background:#3a4a6a; }'
        )
        spec_btn.clicked.connect(self._on_show_spec)

        word_btn = QPushButton('📄 Word复盘报告')
        word_btn.setToolTip('导出追踪复盘报告（.docx），含总览、聚合统计、改进候选和 AI 二次分析提示词')
        word_btn.setStyleSheet(
            'QPushButton { background:#17304a; color:#9ed0ff; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 12px; }'
            'QPushButton:hover { background:#22496f; }'
        )
        word_btn.clicked.connect(self._on_export_word)
        self._word_export_btn = word_btn

        delete_btn = QPushButton('删除选中')
        delete_btn.setToolTip('删除选中的追踪任务\n删除后该任务不再参与胜率、报告和导出统计')
        delete_btn.setStyleSheet(
            'QPushButton { background:#4a1a1a; color:#e88888; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 12px; }'
            'QPushButton:hover { background:#6a2a2a; }'
        )
        delete_btn.clicked.connect(self._on_delete_selected)
        self._delete_btn = delete_btn

        recompute_btn = QPushButton('♻️ 重算评级')
        recompute_btn.setToolTip('用新规则重算所有已结算任务的评级（修正历史看跌/回避单等），会清空旧 AI 反思')
        recompute_btn.setStyleSheet(
            'QPushButton { background:#1a3a3a; color:#88d8d8; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 12px; }'
            'QPushButton:hover { background:#2a5a5a; }'
        )
        recompute_btn.clicked.connect(self._on_recompute_clicked)
        self._recompute_btn = recompute_btn

        _kpi_btns = [spec_btn, word_btn, delete_btn, recompute_btn]

        btn_grid = QGridLayout()
        btn_grid.setHorizontalSpacing(6)
        btn_grid.setVerticalSpacing(5)
        for _i, _b in enumerate(_kpi_btns):
            _b.setFixedHeight(28)
            _b.setMinimumWidth(124)
            btn_grid.addWidget(_b, _i // 2, _i % 2)
        btn_grid.setRowStretch(btn_grid.rowCount(), 1)  # 按钮顶对齐
        kpi_outer.addLayout(btn_grid)

        root.addWidget(kpi_bar)

        # 筛选行
        filter_bar = QWidget()
        filter_bar.setFixedHeight(34)
        filter_bar.setStyleSheet('background:#0d1525; border-bottom:1px solid #1a2a3a;')
        fb_lay = QHBoxLayout(filter_bar)
        fb_lay.setContentsMargins(8, 4, 8, 4)
        fb_lay.setSpacing(6)

        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        for key, label, checked in [
            ('all', '全部', True), ('open', '进行中', False), ('closed', '已完成', False),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(checked)
            btn.setFixedHeight(24)
            btn.setStyleSheet(
                'QPushButton { background:#111a30; color:#888; border:none;'
                ' border-radius:3px; font-size:11px; padding:0 10px; }'
                'QPushButton:checked { background:#1a2a4a; color:#c8d8f8; }'
                'QPushButton:hover:!checked { color:#aaa; }'
            )
            btn.clicked.connect(lambda _c, k=key: self._on_filter_changed(k))
            self._filter_group.addButton(btn)
            fb_lay.addWidget(btn)

        fb_lay.addSpacing(20)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText('🔍 搜索代码 / 名称')
        self._search_edit.setFixedHeight(24)
        self._search_edit.setMaximumWidth(220)
        self._search_edit.setStyleSheet(
            'QLineEdit { background:#0a0f1a; color:#e0e0e0; border:1px solid #1a2a3a;'
            ' border-radius:3px; padding:0 6px; font-size:11px; }'
        )
        self._search_edit.textChanged.connect(self._on_search_changed)
        fb_lay.addWidget(self._search_edit)

        self._retro_status_lbl = QLabel('')
        self._retro_status_lbl.setStyleSheet('color:#88aacc; font-size:11px; padding-left:8px;')
        fb_lay.addWidget(self._retro_status_lbl)

        fb_lay.addStretch()

        self._batch_retro_btn = QPushButton('🧠 批量复盘')
        self._batch_retro_btn.setFixedHeight(24)
        self._batch_retro_btn.setStyleSheet(
            'QPushButton { background:#3a2a5a; color:#c8aae8; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 10px; }'
            'QPushButton:hover { background:#4a3a6a; }'
            'QPushButton:disabled { color:#55667a; }'
        )
        self._batch_retro_btn.clicked.connect(self._on_batch_retro_clicked)
        fb_lay.addWidget(self._batch_retro_btn)

        refresh_btn = QPushButton('🔄 刷新')
        refresh_btn.setFixedHeight(24)
        refresh_btn.setStyleSheet(
            'QPushButton { background:#2a3a5a; color:#aac8e8; border:none;'
            ' border-radius:3px; font-size:11px; padding:0 10px; }'
            'QPushButton:hover { background:#3a4a6a; }'
        )
        refresh_btn.clicked.connect(self.refresh)
        self._refresh_btn = refresh_btn
        fb_lay.addWidget(refresh_btn)

        self._auto_settle_cb = QCheckBox('开机自动结算')
        self._auto_settle_cb.setChecked(bool(load_setting('auto_settle_on_startup', True)))
        self._auto_settle_cb.setStyleSheet('color:#88aacc; font-size:11px; padding-left:6px;')
        self._auto_settle_cb.stateChanged.connect(
            lambda st: save_setting('auto_settle_on_startup', bool(st)))
        fb_lay.addWidget(self._auto_settle_cb)

        root.addWidget(filter_bar)

        # 表格
        self._table = _TrackingTable()
        self._table.row_double_clicked.connect(self._on_row_double_clicked)
        root.addWidget(self._table, stretch=1)

    # ── 数据 ─────────────────────────────────────────
    def _load_tasks(self):
        self._tasks = load_tasks()
        self.point_count_changed.emit(len(self._tasks))
        self._reload_table()
        self._update_kpis()
        self._update_retro_status()

    def _update_retro_status(self):
        pending = len(pending_retro_tasks(self._tasks))
        lbl = getattr(self, '_retro_status_lbl', None)
        if lbl is not None:
            lbl.setText(
                f'待复盘 {pending} ｜ 运行 {len(self._retro_workers)} ｜ 排队 {len(self._auto_retro_queue)}'
            )
        btn = getattr(self, '_batch_retro_btn', None)
        if btn is not None:
            busy = bool(self._retro_workers or self._auto_retro_queue)
            if busy:
                btn.setEnabled(True)
                btn.setText('▶ 继续复盘' if self._retro_paused else f'⏸ 暂停 {len(self._auto_retro_queue)}')
            else:
                btn.setEnabled(pending > 0)
                btn.setText(f'🧠 批量复盘 {pending}' if pending else '🧠 批量复盘')

    def _reload_table(self):
        tasks = self._tasks
        if self._filter_mode == 'open':
            tasks = [t for t in tasks if t.get('status') == 'open']
        elif self._filter_mode == 'closed':
            tasks = [t for t in tasks if t.get('status') == 'closed']
        q = self._search_text.strip().lower()
        if q:
            tasks = [t for t in tasks
                     if q in t.get('code', '').lower() or q in t.get('name', '').lower()]
        self._table.load(tasks, self._current_prices)

    def _update_kpis(self):
        stats = get_tracking_stats(self._tasks, index_df=benchmark_index_df())
        open_n   = stats['open_count']
        closed_n = stats['closed_count']
        total_n  = stats['total']
        gd = stats['grade_dist']

        # 行 1：核心 KPI（个股胜率 / 板块胜率 / 达标率 / 总数 / 进行中）
        stock_closed_n = stats.get('graded_stock_count', 0)
        swr = stats.get('sector_win_rate')
        swr_n = stats.get('sector_win_count', 0)
        s_closed_n = stats.get('sector_closed_count', 0)

        bc = stats.get('by_class', {})

        def _cls_text(label, key):
            c = bc.get(key, {})
            r = c.get('rate')
            tot = c.get('total', 0)
            if r is not None:
                return f'{label} {r*100:.0f}%（{c.get("win", 0)}/{tot}）'
            return f'{label} —（{tot}）'

        if swr is not None:
            swr_text = f'板块 {swr*100:.0f}%（{swr_n}/{s_closed_n}）'
        elif s_closed_n > 0:
            swr_text = f'板块 积累中（{s_closed_n}/3）'
        else:
            swr_text = '板块 —'
        conv = stats.get('conversion') or {}
        conv_rate = conv.get('rate')
        conv_total = conv.get('total_candidates', 0)
        conv_text = (
            f'候选转换 {conv_rate*100:.0f}%（{conv.get("converted", 0)}/{conv_total}）'
            if conv_rate is not None else f'候选转换 —（{conv_total}）'
        )
        suspect_n = (stats.get('fill_suspect') or {}).get('total', 0)
        suspect_text = f'疑似不可成交 {suspect_n}'
        self._kpi_lbl.setText(
            f'{_cls_text("看多兑现", "buy")}  |  {_cls_text("回避有效", "avoid")}  |  '
            f'{_cls_text("观察合理", "watch")}  |  {conv_text}  |  {suspect_text}  |  {swr_text}  |  总数 {total_n}  |  '
            f'进行中 {open_n}  |  已完成 {closed_n}'
        )
        self._kpi_lbl3.setText(format_skill_kpi_html(stats.get('skill_stats')))

        # 行 2：评级分布 + 中文路径质量
        closed_tasks = [t for t in self._tasks if t.get('status') == 'closed']
        path_dist = {'P_clean': 0, 'P_tested': 0, 'P_stressed': 0, 'neutral': 0}
        for t in closed_tasks:
            d = t.get('grade_detail') or {}
            p = d.get('path_quality')
            if p in path_dist:
                path_dist[p] += 1

        if closed_n > 0:
            grade_html = ' '.join(
                f'<span style="color:{_GRADE_COLOR.get(g, "#888")};">'
                f'{g}×{n}</span>'
                for g, n in gd.items() if n > 0
            ) or '<span style="color:#666;">—</span>'

            def _pct(p):
                return f'{p/closed_n*100:.0f}%' if closed_n else '—'
            path_html = '  '.join(
                f'<span style="color:{_PATH_QUALITY_COLOR[key]};">'
                f'{_PATH_QUALITY_LABEL[key]} {path_dist[key]} ({_pct(path_dist[key])})</span>'
                for key in ('neutral', 'P_clean', 'P_tested', 'P_stressed')
                if path_dist[key] > 0
            ) or (
                '<span style="color:#666;">暂无路径记录</span>'
            )

            self._kpi_lbl2.setText(
                f'<span style="color:#aab;">评级</span> {grade_html}'
                f'&nbsp;&nbsp;&nbsp;&nbsp;<span style="color:#445566;">|</span>&nbsp;&nbsp;&nbsp;&nbsp;'
                f'<span style="color:#aab;">路径</span> {path_html}'
            )
        else:
            self._kpi_lbl2.setText(
                '<span style="color:#666;">尚无已完成任务，结算后这里将展示评级分布、'
                '路径质量</span>'
            )

    # ── 事件 ─────────────────────────────────────────
    def _on_filter_changed(self, key: str):
        self._filter_mode = key
        self._reload_table()

    def _on_search_changed(self, text: str):
        self._search_text = text
        self._reload_table()

    def _on_show_spec(self):
        SpecDialog(self).exec()

    def _on_export_word(self):
        """导出 Word 复盘报告。"""
        if self._word_export_worker and self._word_export_worker.isRunning():
            QMessageBox.information(self, '正在导出', 'Word 复盘报告正在后台导出，请稍候。')
            return

        dlg = QDialog(self)
        dlg.setWindowTitle('导出 Word 复盘报告')
        dlg.setMinimumWidth(360)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        range_box = QComboBox()
        for label, value in [('最近30天', 30), ('最近7天', 7), ('最近90天', 90), ('全部', 'all')]:
            range_box.addItem(label, value)
        sample_box = QComboBox()
        for label, value in [('全部样本', 'all'), ('虚拟观察单', 'watch')]:
            sample_box.addItem(label, value)
        kind_box = QComboBox()
        for label, value in [('全部标的', 'all'), ('个股', 'stock'), ('ETF', 'etf'), ('板块', 'sector')]:
            kind_box.addItem(label, value)
        detail_chk = QCheckBox('包含逐笔明细')
        detail_chk.setChecked(True)
        ai_chk = QCheckBox('生成 AI 总评（消耗 API）')
        ai_chk.setChecked(False)

        for title, widget in [
            ('时间范围', range_box),
            ('样本范围', sample_box),
            ('标的范围', kind_box),
        ]:
            lay.addWidget(QLabel(title))
            lay.addWidget(widget)
        lay.addWidget(detail_chk)
        lay.addWidget(ai_chk)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton('导出')
        cancel_btn = QPushButton('取消')
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, '保存 Word 复盘报告', 'tracking_review_report.docx',
            'Word 文档 (*.docx)',
        )
        if not path:
            return
        if not path.lower().endswith('.docx'):
            path += '.docx'

        try:
            ensure_report_target_writable(path)
        except RuntimeError as e:
            QMessageBox.warning(self, '文件正在使用', str(e))
            return

        options = {
            'range_days': range_box.currentData(),
            'sample_scope': sample_box.currentData(),
            'kind_scope': kind_box.currentData(),
            'include_records': detail_chk.isChecked(),
        }

        worker = _WordExportWorker(
            path,
            options,
            ai_chk.isChecked(),
            load_api_key() or '',
            parent=self,
        )
        worker.completed.connect(self._on_word_export_done)
        worker.failed.connect(self._on_word_export_error)

        def _cleanup():
            if self._word_export_btn:
                self._word_export_btn.setEnabled(True)
                self._word_export_btn.setText('📄 Word复盘报告')
            self._word_export_worker = None

        worker.finished.connect(_cleanup)
        self._word_export_worker = worker
        if self._word_export_btn:
            self._word_export_btn.setEnabled(False)
            self._word_export_btn.setText('📄 正在导出…')
        self.bottom_status_changed.emit('Word复盘报告正在后台导出，请稍候')
        worker.start()

    def _on_word_export_done(self, result: dict):
        QMessageBox.information(
            self, '导出成功',
            f'已导出 {result["record_count"]} 条样本\n'
            f'改进候选 {result["candidate_count"]} 条\n{result["path"]}',
        )
        self.bottom_status_changed.emit(
            f'Word复盘报告已导出: {result["record_count"]} 条样本',
        )

    def _on_word_export_error(self, error: str):
        title = '文件正在使用' if '目标文件正在被占用' in error else 'Word 导出失败'
        QMessageBox.warning(self, title, error)
        self.bottom_status_changed.emit(f'Word复盘报告导出失败: {error}')

    def _selected_task(self):
        """从当前表格选中行定位任务对象；无选中或找不到返回 None。"""
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        if item is None:
            return None
        task = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(task, dict):
            return None
        return task

    def _set_delete_multi_mode(self, enabled: bool, *, clear_selection: bool = False):
        self._delete_multi_mode = enabled
        self._table.set_multi_delete_mode(enabled)
        if clear_selection:
            self._table.clearSelection()
            self._table.setCurrentCell(-1, -1)
        btn = self._delete_btn
        if btn is None:
            return
        if enabled:
            btn.setText('一键删除')
            btn.setToolTip('逐行点击多选要删除的追踪任务，再点击一键删除')
            btn.setStyleSheet(
                'QPushButton { background:#7a241f; color:#ffd0c8; border:none;'
                ' border-radius:3px; font-size:11px; padding:0 12px; font-weight:bold; }'
                'QPushButton:hover { background:#9a342f; }'
            )
        else:
            btn.setText('删除选中')
            btn.setToolTip('进入多选删除模式\n删除后任务不再参与胜率、报告和导出统计')
            btn.setStyleSheet(
                'QPushButton { background:#4a1a1a; color:#e88888; border:none;'
                ' border-radius:3px; font-size:11px; padding:0 12px; }'
                'QPushButton:hover { background:#6a2a2a; }'
            )

    def _on_delete_selected(self):
        selected = self._table.selected_tasks()
        action = _delete_click_action(self._delete_multi_mode, len(selected))
        if action == 'enter_multi':
            self._set_delete_multi_mode(True, clear_selection=True)
            self.bottom_status_changed.emit('已进入批量删除模式：多选追踪单后点击“一键删除”')
            return
        if action == 'need_selection':
            QMessageBox.information(self, '未选择记录', '请多选要删除的追踪记录，再点击“一键删除”')
            return

        preview_lines = []
        for task in selected[:8]:
            grade = task.get('grade') or '未评级'
            preview_lines.append(
                f'- {task.get("name", "")} ({task.get("code", "")})  '
                f'{task.get("status", "")} / {grade}'
            )
        if len(selected) > 8:
            preview_lines.append(f'... 还有 {len(selected) - 8} 条')

        confirm = QMessageBox.question(
            self,
            '确认删除',
            f'确定要删除选中的 {len(selected)} 条追踪记录吗？\n\n'
            + '\n'.join(preview_lines)
            + '\n\n'
            '删除后该任务不再参与胜率、报告和导出统计，历史表现可能因此变化。',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        selected_ids = [str(task.get('id') or '') for task in selected if task.get('id')]
        result = delete_tasks(selected_ids)
        deleted = result.get('tasks') or []
        missing = set(result.get('missing') or [])
        failed = [
            f'{task.get("name", "")}({task.get("code", "")}): not_found'
            for task in selected
            if str(task.get('id') or '') in missing
        ]

        if self._open_detail_dlg is not None and self._open_detail_dlg.isVisible():
            deleted_ids = {t.get('task_id') or t.get('id') for t in deleted}
            if self._open_detail_dlg._task.get('id') in deleted_ids:
                self._open_detail_dlg.close()

        self._set_delete_multi_mode(False)
        self._load_tasks()
        if failed:
            QMessageBox.warning(self, '部分删除失败', '\n'.join(failed[:10]))
        self.bottom_status_changed.emit(f'已删除 {len(deleted)} 条追踪记录')

    def _on_row_double_clicked(self, task: dict):
        cur = (self._current_prices.get(task.get('code', '')) or {}).get('price')
        cur = float(cur) if cur is not None else None
        dlg = TrackingDetailDialog(task, cur, self)
        mw = self.window()
        if hasattr(mw, '_on_intel_anchor'):
            dlg.intel_anchor_clicked.connect(mw._on_intel_anchor)
        dlg.regenerate_retro_requested.connect(self._start_retro_worker)
        dlg.task_link_requested.connect(self._open_task_by_id)
        self._open_detail_dlg = dlg
        dlg.exec()
        self._open_detail_dlg = None

    def _open_task_by_id(self, task_id: str):
        task = next((t for t in self._tasks if str(t.get('id') or '') == task_id), None)
        if task is None:
            self._tasks = load_tasks()
            task = next((t for t in self._tasks if str(t.get('id') or '') == task_id), None)
        if task is None:
            return
        if self._open_detail_dlg is not None:
            self._open_detail_dlg.accept()
        QTimer.singleShot(0, lambda t=task: self._on_row_double_clicked(t))

    # ── 反思 Worker ──────────────────────────────────
    def _drain_auto_retro_queue(self):
        api_key = load_api_key() or ''
        if not api_key or not self._auto_retro_queue:
            return
        if self._retro_paused:
            self._update_retro_status()
            return
        while self._auto_retro_queue and len(self._retro_workers) < _AUTO_RETRO_MAX_CONCURRENT:
            task = self._auto_retro_queue.pop(0)
            task_id = str(task.get('id') or '')
            self._auto_retro_started.add(task_id)
            self._start_retro_worker(task, api_key=api_key, auto=True)
        self._update_retro_status()

    def _on_batch_retro_clicked(self):
        if self._retro_workers or self._auto_retro_queue:
            self._retro_paused = not self._retro_paused
            if not self._retro_paused:
                self._drain_auto_retro_queue()
            self._update_retro_status()
            return
        api_key = load_api_key() or ''
        if not api_key:
            QMessageBox.information(self, '提示', '未配置 API Key，无法生成 AI 反思')
            return
        pending = pending_retro_tasks(self._tasks)
        if not pending:
            QMessageBox.information(self, '提示', '没有待复盘的任务')
            return
        self._retro_paused = False
        for task in pending:
            task_id = str(task.get('id') or '')
            if not task_id or task_id in self._auto_retro_started:
                continue
            if any(str(t.get('id') or '') == task_id for t in self._auto_retro_queue):
                continue
            self._auto_retro_queue.append(task)
        self._update_retro_status()
        self._drain_auto_retro_queue()

    def _start_retro_worker(self, task: dict, api_key: str | None = None, auto: bool = False):
        if task.get('grade') is None:
            if not auto:
                self._reset_open_dlg_retro_btn('（任务尚未评级，无法生成反思）')
            return
        api_key = api_key if api_key is not None else (load_api_key() or '')
        if not api_key:
            if not auto:
                self._reset_open_dlg_retro_btn('（未配置 API Key，无法生成反思）')
            return
        append_tracking_event(
            'retrospective_started',
            task_id=task.get('id', ''),
            code=task.get('code', ''),
            name=task.get('name', ''),
            source=task.get('source', 'local'),
            message='自动生成单笔 AI 反思' if auto else '开始生成单笔 AI 反思',
            payload={'grade': task.get('grade'), 'auto': auto},
        )
        w = _RetrospectiveWorker(task, api_key, parent=self)
        w.finished.connect(self._on_retro_done)
        w.error.connect(self._on_retro_error)
        def _cleanup():
            if w in self._retro_workers:
                self._retro_workers.remove(w)
            self._update_retro_status()
            QTimer.singleShot(0, self._drain_auto_retro_queue)
        w.finished.connect(_cleanup)
        w.error.connect(_cleanup)
        self._retro_workers.append(w)
        w.start()
        self._update_retro_status()

    def _reset_open_dlg_retro_btn(self, msg: str | None = None):
        dlg = self._open_detail_dlg
        if dlg is None or not dlg.isVisible():
            return
        btn = getattr(dlg, '_retro_btn', None)
        if btn is not None:
            btn.setEnabled(True)
            btn.setText('🔄 重新生成反思' if dlg._task.get('retrospective') else '✨ 生成 AI 反思')
        browser = getattr(dlg, '_retro_browser', None)
        if msg and browser is not None and not dlg._task.get('retrospective'):
            browser.setPlainText(msg)

    def _on_retro_error(self, task_id: str, error: str):
        logging.getLogger(__name__).warning('[retro] %s: %s', task_id, error)
        dlg = self._open_detail_dlg
        if dlg is not None and dlg.isVisible() and dlg._task.get('id') == task_id:
            self._reset_open_dlg_retro_btn(f'（生成失败：{error[:120]}）')
        task = next((t for t in self._tasks if t.get('id') == task_id), {})
        append_tracking_event(
            'retrospective_failed',
            task_id=task_id,
            code=task.get('code', ''),
            name=task.get('name', ''),
            source=task.get('source', 'local'),
            severity='warning',
            message='单笔 AI 反思生成失败',
            payload={'error': error},
        )

    def _on_retro_done(self, task_id: str, text: str):
        import re as _re
        _ERR_RE   = _re.compile(r'\[错误类型:\s*([ABCDN])\]')
        _CAUSE_RE = _re.compile(r'核心归因[:：]\s*(.+?)(?:\n|$)')
        head = text.strip()[:300]
        err_m   = _ERR_RE.search(head)
        cause_m = _CAUSE_RE.search(head)
        err_type   = err_m.group(1) if err_m else None
        root_cause = (cause_m.group(1).strip()[:60] if cause_m else None)

        fields: dict = {'retrospective': text}
        if err_type:
            fields['error_type'] = err_type
        if root_cause:
            fields['root_cause'] = root_cause
        found_task = persist_task_fields(task_id, fields)
        for task in self._tasks:
            if task.get('id') == task_id:
                task.update(fields)
                break

        if found_task is not None:
            append_tracking_event(
                'retrospective_completed',
                task_id=task_id,
                code=found_task.get('code', ''),
                name=found_task.get('name', ''),
                source=found_task.get('source', 'local'),
                message='单笔 AI 反思生成完成',
                payload={'error_type': err_type, 'root_cause': root_cause},
            )
            try:
                from core.analysis_memory import append_retrospective
                snap = found_task.get('pred_snapshot') or {}
                pid = snap.get('prediction_id') or found_task.get('id', '')
                if found_task.get('kind') == 'sector':
                    mem_code = f'sector:{found_task.get("name") or found_task.get("code", "")}'
                else:
                    mem_code = found_task.get('code', '')
                append_retrospective(mem_code, pid, text, err_type, root_cause)
            except Exception:
                pass
        dlg = self._open_detail_dlg
        if dlg is not None and dlg.isVisible() and dlg._task.get('id') == task_id:
            dlg.update_retrospective(text)

    # ── 公开接口 ─────────────────────────────────────
    def refresh(self):
        if self._settlement_worker and self._settlement_worker.isRunning():
            return
        self._load_tasks()
        self._settle_attempted.clear()
        self._convert_cooldown.clear()
        self._initial_sweep_done = False  # 手动刷新做一次完整 K 线扫描
        self._set_refresh_busy(manual=True)
        self.request_maintenance()

    def auto_refresh(self):
        if not is_trading_time():
            return
        if self._settlement_worker and self._settlement_worker.isRunning():
            return
        now = time.monotonic()
        if now - self._last_auto_maintenance_ts < _AUTO_MAINTENANCE_INTERVAL_S:
            return
        self._last_auto_maintenance_ts = now
        self._load_tasks()
        self._settle_attempted.clear()
        self._initial_sweep_done = True
        self._set_refresh_busy(manual=False)
        self.request_maintenance()

    def post_close_refresh(self):
        if self._settlement_worker and self._settlement_worker.isRunning():
            return
        today_key = date.today().isoformat()
        if self._post_close_maintenance_date == today_key:
            return
        self._post_close_maintenance_date = today_key
        self._load_tasks()
        self._settle_attempted.clear()
        self._convert_cooldown.clear()
        self._initial_sweep_done = False
        self._set_refresh_busy(manual=False)
        self.request_maintenance()

    def _set_refresh_busy(self, manual: bool = False):
        if self._refresh_in_progress:
            return
        self._refresh_in_progress = True
        self._refresh_is_manual = manual
        self._refresh_done = 0
        if not manual:
            return
        btn = getattr(self, '_refresh_btn', None)
        if btn:
            btn.setEnabled(False)
            btn.setText('⏳ 刷新中 0 条')

    def _finish_refresh_feedback(self):
        if not self._refresh_in_progress:
            return
        self._refresh_in_progress = False
        if not self._refresh_is_manual:
            self.status_changed.emit(
                'success', f'上次刷新: {datetime.now().strftime("%H:%M:%S")}')
            return
        btn = getattr(self, '_refresh_btn', None)
        if btn:
            btn.setText(f'✓ 刷新完成 {getattr(self, "_refresh_done", 0)} 条')
            QTimer.singleShot(1500, self._restore_refresh_button)

    def _restore_refresh_button(self):
        btn = getattr(self, '_refresh_btn', None)
        if btn and not (self._settlement_worker and self._settlement_worker.isRunning()):
            btn.setEnabled(True)
            btn.setText('🔄 刷新')

    def request_maintenance(self):
        self._settle_next_batch()

    def set_current_prices(self, prices: dict):
        self._current_prices = prices
        self._table.update_floating_pnl(prices)
        if not load_setting('auto_settle_on_startup', True):
            return
        # 首次延后启动，之后每轮推价都跑一遍闸门（廉价：现价未到触发价直接跳过）
        if not self._settle_started:
            self._settle_started = True
            self._set_refresh_busy(manual=False)
            QTimer.singleShot(_SETTLE_STARTUP_DELAY_MS, self._settle_next_batch)
        else:
            self.auto_refresh()

    def _settle_next_batch(self):
        if self._settlement_worker and self._settlement_worker.isRunning():
            return
        now = time.monotonic()
        skip = {c for c, ts in self._convert_cooldown.items() if now - ts < _CONVERT_COOLDOWN_S}
        # 启动首扫不设价闸门（补抓软件关闭期间已越触发价/冲高回落的候选）；
        # 首扫完成后，盘中复查只放行现价已到触发价的候选，省掉无谓 K 线拉取
        gate_prices = self._current_prices if self._initial_sweep_done else None
        due = pending_maintenance_ids(
            self._tasks,
            attempted=self._settle_attempted,
            current_prices=gate_prices,
            skip_codes=skip,
            batch_code_limit=_SETTLE_BATCH,
        )
        if not due:
            self._initial_sweep_done = True
            self._finish_refresh_feedback()
            return
        batch = set(due)
        # 候选变身只记代码冷却（留待价升后复查）；到期结算单进永久 attempted（终态去重）
        for tid in due:
            task = next((t for t in self._tasks if str(t.get('id')) == tid), None)
            if task is None:
                continue
            if _is_convertible_watch(task):
                self._convert_cooldown[str(task.get('code') or '')] = now
                self._settle_attempted.add(tid)
            else:
                try:
                    deadline_due = date.fromisoformat(str(task.get('deadline') or '')[:10]) <= date.today()
                except ValueError:
                    deadline_due = False
                is_early_tp = task.get('status') == 'open' and task.get('task_class') == 'buy'
                if deadline_due or task.get('tp_pending_recheck') or is_early_tp:
                    self._settle_attempted.add(tid)
        try:
            prices = {
                code: dict(value) if isinstance(value, dict) else value
                for code, value in self._current_prices.items()
            }
            worker = _SettlementWorker(
                batch,
                prices,
                parent=self,
            )
            worker.completed.connect(self._on_settlement_done)
            worker.failed.connect(self._on_settlement_error)
            worker.finished.connect(lambda: setattr(self, '_settlement_worker', None))
            self._settlement_worker = worker
            worker.start()
        except Exception as e:
            logging.getLogger(__name__).warning('[tracking] 结算检查失败: %s', e)
            self._finish_refresh_feedback()

    def _on_settlement_done(self, result: dict):
        changed = bool(
            (result or {}).get('settled')
            or (result or {}).get('converted')
            or (result or {}).get('conversion_wait_changed')
            or (result or {}).get('recheck_dropped')
        )
        if changed:
            self._tasks = load_tasks()
            self._reload_table()
            self._update_kpis()
            self._update_retro_status()
        if self._refresh_in_progress:
            self._refresh_done += int((result or {}).get('processed_codes', 0) or 0)
            if self._refresh_is_manual:
                btn = getattr(self, '_refresh_btn', None)
                if btn:
                    btn.setText(f'⏳ 刷新中 {self._refresh_done} 条')
        QTimer.singleShot(_SETTLE_BATCH_GAP_MS, self._settle_next_batch)

    def _on_settlement_error(self, error: str):
        logging.getLogger(__name__).warning('[tracking] settlement check failed: %s', error)
        QTimer.singleShot(_SETTLE_BATCH_GAP_MS, self._settle_next_batch)

    def _on_recompute_clicked(self):
        if self._recompute_worker and self._recompute_worker.isRunning():
            return
        targets = [t for t in self._tasks
                   if t.get('status') == 'closed' and t.get('kind') != 'sector']
        if not targets:
            QMessageBox.information(self, '提示', '没有可重算的已结算任务')
            return
        reply = QMessageBox.question(
            self, '重算历史评级',
            f'将用新规则重算 {len(targets)} 条已结算任务，并清空它们的旧 AI 反思（需重新生成）。继续？')
        if reply != QMessageBox.StandardButton.Yes:
            return
        worker = _RecomputeWorker(deepcopy(self._tasks), parent=self)
        worker.progress.connect(self._on_recompute_progress)
        worker.completed.connect(self._on_recompute_done)
        worker.finished.connect(lambda: setattr(self, '_recompute_worker', None))
        self._recompute_worker = worker
        worker.start()
        btn = getattr(self, '_recompute_btn', None)
        if btn:
            btn.setEnabled(False)
            btn.setText(f'⏳ 重算中 0/{len(targets)}')
        self.bottom_status_changed.emit(f'正在重算 {len(targets)} 条历史评级…')

    def _on_recompute_progress(self, done: int, total: int):
        btn = getattr(self, '_recompute_btn', None)
        if btn:
            btn.setText(f'⏳ 重算中 {done}/{total}')
        self.bottom_status_changed.emit(f'重算历史评级 {done}/{total}…')

    def _on_recompute_done(self, success: int, total: int):
        self._tasks = load_tasks()
        self._reload_table()
        self._update_kpis()
        self._update_retro_status()
        btn = getattr(self, '_recompute_btn', None)
        if btn:
            btn.setEnabled(True)
            btn.setText('♻️ 重算评级')
        self.bottom_status_changed.emit(f'已按新口径重算：{success}/{total} 条已更新，看多兑现率分母变化是预期结果')
        QMessageBox.information(self, '已按新口径重算', f'已按新口径重算 {success}/{total} 条历史评级；看多兑现率分母变化是预期结果。')
