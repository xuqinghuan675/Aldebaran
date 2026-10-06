"""3D stock intelligence network panel."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.qt_runtime import configure_qt_runtime
configure_qt_runtime()

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.cache import load_watchlist
from core.data_worker import DataWorker
from core.evidence_ai import analyze_evidence_pack
from core.evidence_graph import EvidencePack
from core.evidence_seed_builder import build_evidence_pack_from_seed
from core.intelligence_feed_service import build_stock_graph_seed
from core.portfolio_data import load_holdings
from ui.evidence_graph_view import EvidenceGraphView


BG0 = '#07101f'
BG1 = '#0c1730'
BG2 = '#111c38'
BORDER = '#22365e'
BORDER_HI = '#2d4675'
TEXT = '#d9e6ff'
MUTED = '#93a4c8'
CYAN = '#32f6d2'

_FIELD_COLORS = {
    'macro': '#2d6cdf',
    'policy': '#a76dff',
    'sector': '#4fc3ff',
    'financial': '#ffd166',
    'demand': '#2ee6a6',
    'trading_behavior': '#31f28a',
    'risk': '#ff4d5e',
    'missing': '#6f7f99',
    'forecast': '#bffcff',
}

_FIELD_GROUPS = [
    ('外部环境', ['macro', 'policy', 'geopolitics_trade', 'liquidity'], [
        '宏观环境', '政策监管', '地缘贸易', '利率汇率', '流动性', '大宗商品',
    ]),
    ('行业链条', ['sector', 'cost', 'price', 'inventory', 'capacity', 'competition'], [
        '行业景气', '供需', '成本', '产品价格', '库存', '产能', '竞争格局', '板块联动',
    ]),
    ('公司经营', ['company', 'financial', 'order_contract', 'shareholder'], [
        '公司公告', '财报经营', '收入结构', '成本结构', '毛利率', '订单合同', '产能投放', '管理层股东',
    ]),
    ('需求出口', ['demand', 'export', 'customer_supplier'], [
        '国内需求', '海外需求', '出口外贸', '终端销量', '渠道库存', '客户资本开支',
    ]),
    ('资金与交易行为情报', ['trading_behavior'], [
        '资金流向', '游资席位', '龙虎榜', '机构行为', '北向ETF', '融资融券', '大宗交易', '基金持仓变化',
    ]),
    ('风险与证据质量', ['risk', 'missing'], [
        '风险事件', '监管处罚', '法律诉讼', '舆情危机', '数据缺失', '证据冲突', '来源可信度', '事件日历',
    ]),
    ('预测变量', ['expectation', 'forecast'], [
        '主导变量', '变化速度', '预期差', '传导路径', '观察清单',
    ]),
]


class IntelligenceNetworkPanel(QWidget):
    status_changed = Signal(str, str)
    point_count_changed = Signal(int)
    bottom_status_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sector_panel_ref = None
        self._current_pack: EvidencePack | None = None
        self._graph_worker: DataWorker | None = None
        self._point_count = 0
        self._current_code = ''
        self._current_name = ''
        self._field_buttons: list[QPushButton] = []
        self._field_button_specs: list[dict[str, Any]] = []
        self._setup_ui()
        self._load_object_lists()
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        target = self._current_target()
        if not target:
            self.status_changed.emit('error', '请选择或输入一个股票代码')
            return
        code, name = target
        if self._graph_worker and self._graph_worker.isRunning():
            return
        self._current_code = code
        self._current_name = name
        self._set_loading(True, f'正在构建 {code} {name} 的情报网络…')
        self._graph_worker = DataWorker(
            fetcher=lambda c=code, n=name: self._build_pack(c, n),
            parent=self,
        )
        self._graph_worker.data_ready.connect(self._on_pack_ready)
        self._graph_worker.error_occurred.connect(self._on_pack_error)
        self._graph_worker.start()

    def auto_refresh(self):
        if self._current_pack is None:
            self.refresh()
            return
        self._graph_view.set_graph(self._current_pack.graph_json)

    def reapply_font(self):
        self._render_current_pack()

    def current_point_count(self) -> int:
        return self._point_count

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setStyleSheet(f'QWidget {{ background-color: {BG0}; color: {TEXT}; }}')

        header = QWidget()
        header.setFixedHeight(50)
        header.setStyleSheet(f'background-color:{BG1}; border-bottom:1px solid {BORDER};')
        header_lay = QHBoxLayout(header)
        header_lay.setContentsMargins(14, 0, 14, 0)
        header_lay.setSpacing(8)

        title = QLabel('情报网络')
        title.setStyleSheet(f'color:{TEXT}; font-size:16px; font-weight:bold;')
        header_lay.addWidget(title)
        self._subject_lbl = QLabel('未选择')
        self._subject_lbl.setStyleSheet(f'color:{MUTED}; font-size:12px;')
        header_lay.addWidget(self._subject_lbl, stretch=1)

        self._refresh_btn = QPushButton('刷新')
        self._refresh_btn.setFixedWidth(70)
        self._refresh_btn.clicked.connect(self.refresh)
        header_lay.addWidget(self._refresh_btn)

        self._ai_btn = QPushButton('AI分析此图')
        self._ai_btn.setFixedWidth(100)
        self._ai_btn.clicked.connect(self._run_pack_analysis)
        header_lay.addWidget(self._ai_btn)

        header_lay.addWidget(QLabel('性能'))
        self._perf_combo = QComboBox()
        self._perf_combo.addItems(['效率', '性能'])
        self._perf_combo.setCurrentText('性能')
        self._perf_combo.setFixedWidth(74)
        self._perf_combo.currentIndexChanged.connect(self._on_performance_changed)
        header_lay.addWidget(self._perf_combo)

        self._status_lbl = QLabel('就绪')
        self._status_lbl.setStyleSheet(f'color:{MUTED}; font-size:12px;')
        header_lay.addWidget(self._status_lbl)
        root.addWidget(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(f'QSplitter::handle {{ background-color:{BORDER}; }}')
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_graph_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([250, 760, 330])
        root.addWidget(splitter, stretch=1)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(220)
        panel.setMaximumWidth(310)
        panel.setStyleSheet(f'background-color:{BG1}; border-right:1px solid {BORDER};')
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        search_lbl = QLabel('对象')
        search_lbl.setStyleSheet(f'color:{MUTED}; font-size:12px; font-weight:bold;')
        lay.addWidget(search_lbl)

        search_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText('代码 名称，例如 600584 长电科技')
        self._search_edit.returnPressed.connect(self._on_search_commit)
        self._search_edit.setStyleSheet(
            f'background:{BG0}; color:{TEXT}; border:1px solid {BORDER}; '
            'border-radius:4px; padding:5px 8px;'
        )
        search_row.addWidget(self._search_edit, stretch=1)
        go_btn = QPushButton('定位')
        go_btn.setFixedWidth(52)
        go_btn.clicked.connect(self._on_search_commit)
        search_row.addWidget(go_btn)
        lay.addLayout(search_row)

        self._holdings_list = self._make_stock_list()
        self._watchlist_list = self._make_stock_list()
        lay.addWidget(self._section_label('持仓'))
        lay.addWidget(self._holdings_list, stretch=1)
        lay.addWidget(self._section_label('自选'))
        lay.addWidget(self._watchlist_list, stretch=1)

        hint = QLabel('板块仅作为个股图里的关联节点，板块中心图留到 v2。')
        hint.setWordWrap(True)
        hint.setStyleSheet(f'color:{MUTED}; font-size:11px; line-height:140%;')
        lay.addWidget(hint)
        return panel

    def _build_graph_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(f'background-color:{BG0};')
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._graph_view = EvidenceGraphView(panel)
        lay.addWidget(self._graph_view, stretch=1)
        self._meta_bar = QLabel('等待情报图谱')
        self._meta_bar.setFixedHeight(28)
        self._meta_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._meta_bar.setStyleSheet(
            f'background:{BG1}; color:{MUTED}; border-top:1px solid {BORDER}; font-size:11px;'
        )
        lay.addWidget(self._meta_bar)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(290)
        panel.setMaximumWidth(390)
        panel.setStyleSheet(f'background-color:{BG1}; border-left:1px solid {BORDER};')
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        evidence_title = QLabel('证据字段')
        evidence_title.setStyleSheet(f'color:{TEXT}; font-size:13px; font-weight:bold;')
        lay.addWidget(evidence_title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f'QScrollArea {{ background:{BG1}; border:none; }}')
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(7)
        self._field_group = QButtonGroup(self)
        self._field_group.setExclusive(True)
        for group_title, layers, labels in _FIELD_GROUPS:
            body_lay.addWidget(self._section_label(group_title))
            btn = QPushButton(' / '.join(labels))
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.setStyleSheet(_field_button_style(_field_group_color(layers)))
            btn.setToolTip(' / '.join(labels))
            btn.setProperty('group_title', group_title)
            btn.setProperty('layers', list(layers))
            btn.setProperty('base_labels', list(labels))
            btn.clicked.connect(lambda _checked, ls=list(layers), title=group_title: self._focus_layers(ls, title))
            self._field_group.addButton(btn)
            self._field_buttons.append(btn)
            self._field_button_specs.append({'button': btn, 'title': group_title, 'layers': list(layers), 'labels': list(labels)})
            body_lay.addWidget(btn)
        body_lay.addStretch()
        scroll.setWidget(body)
        lay.addWidget(scroll, stretch=2)

        self._evidence_box = QTextBrowser()
        self._evidence_box.setMinimumHeight(118)
        self._evidence_box.setStyleSheet(_text_box_style())
        lay.addWidget(self._evidence_box, stretch=1)

        analysis_title = QLabel('AI情报裁决')
        analysis_title.setStyleSheet(f'color:{TEXT}; font-size:13px; font-weight:bold;')
        lay.addWidget(analysis_title)
        self._analysis_box = QTextBrowser()
        self._analysis_box.setMinimumHeight(180)
        self._analysis_box.setStyleSheet(_text_box_style())
        lay.addWidget(self._analysis_box, stretch=1)
        return panel

    def _make_stock_list(self) -> QListWidget:
        widget = QListWidget()
        widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        widget.itemClicked.connect(self._on_stock_item_clicked)
        widget.setStyleSheet(
            f'QListWidget {{ background:{BG0}; color:{TEXT}; border:1px solid {BORDER}; '
            'border-radius:4px; padding:3px; }}'
            f'QListWidget::item {{ padding:6px 5px; border-bottom:1px solid #102344; }}'
            f'QListWidget::item:selected {{ background:#123459; color:#f7fbff; }}'
        )
        return widget

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f'color:{MUTED}; font-size:11px; font-weight:bold;')
        return label

    def _load_object_lists(self):
        holdings = _normalize_records(load_holdings())
        watchlist = _normalize_records(load_watchlist())
        if not holdings and not watchlist:
            watchlist = [{'code': '600584', 'name': '长电科技', 'group': '示例'}]
        self._fill_stock_list(self._holdings_list, holdings)
        self._fill_stock_list(self._watchlist_list, watchlist)
        first = holdings[0] if holdings else (watchlist[0] if watchlist else None)
        if first:
            self._current_code = first['code']
            self._current_name = first['name'] or first['code']
            self._search_edit.setText(f'{self._current_code} {self._current_name}')
            self._select_list_item(self._current_code)

    def _fill_stock_list(self, widget: QListWidget, records: list[dict[str, str]]) -> None:
        widget.clear()
        for rec in records:
            code = rec.get('code', '')
            name = rec.get('name') or code
            tag = rec.get('group') or rec.get('theme') or ''
            text = f'{code}  {name}' + (f'\n{tag}' if tag else '')
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, {'code': code, 'name': name})
            widget.addItem(item)
        if not records:
            item = QListWidgetItem('暂无')
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            widget.addItem(item)

    def _on_stock_item_clicked(self, item: QListWidgetItem):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        code = data.get('code')
        if not code:
            return
        self._current_code = code
        self._current_name = data.get('name') or code
        self._search_edit.setText(f'{self._current_code} {self._current_name}')
        self.refresh()

    def _on_search_commit(self):
        text = self._search_edit.text().strip()
        if not text:
            return
        parts = text.replace('，', ' ').replace(',', ' ').split()
        code = _clean_code(parts[0])
        name = parts[1] if len(parts) > 1 else self._lookup_name(code)
        if not code:
            QMessageBox.information(self, '对象无效', '请输入 6 位股票代码。')
            return
        self._current_code = code
        self._current_name = name or code
        self._select_list_item(code)
        self.refresh()

    def _current_target(self) -> tuple[str, str] | None:
        if self._current_code:
            return self._current_code, self._current_name or self._current_code
        text = self._search_edit.text().strip()
        if not text:
            return None
        parts = text.split()
        code = _clean_code(parts[0])
        if not code:
            return None
        name = parts[1] if len(parts) > 1 else self._lookup_name(code)
        return code, name or code

    def _lookup_name(self, code: str) -> str:
        for widget in (self._holdings_list, self._watchlist_list):
            for row in range(widget.count()):
                data = widget.item(row).data(Qt.ItemDataRole.UserRole) or {}
                if data.get('code') == code:
                    return data.get('name') or code
        return code

    def _select_list_item(self, code: str):
        for widget in (self._holdings_list, self._watchlist_list):
            widget.blockSignals(True)
            for row in range(widget.count()):
                item = widget.item(row)
                data = item.data(Qt.ItemDataRole.UserRole) or {}
                item.setSelected(data.get('code') == code)
            widget.blockSignals(False)

    def _build_pack(self, code: str, name: str) -> EvidencePack:
        seed = build_stock_graph_seed(
            code,
            name,
            force_refresh=False,
            sector_context=self._get_sector_context(),
        )
        return build_evidence_pack_from_seed(seed)

    def _get_sector_context(self) -> dict[str, Any] | None:
        ref = self._sector_panel_ref
        if ref is None or not hasattr(ref, 'get_market_context'):
            return None
        try:
            return ref.get_market_context()
        except Exception:
            return None

    def _on_pack_ready(self, pack: EvidencePack):
        self._current_pack = pack
        self._point_count = len(pack.nodes)
        self._set_loading(False, '图谱已更新')
        self._render_current_pack()
        self.point_count_changed.emit(self._point_count)
        self.status_changed.emit('success', f'情报网络已更新：{pack.subject_code} {pack.subject_name}')
        self.bottom_status_changed.emit(f'情报网络：{self._point_count} 节点 / {len(pack.edges)} 关系')

    def _on_pack_error(self, message: str):
        self._set_loading(False, '图谱构建失败')
        self.status_changed.emit('error', message)
        self.bottom_status_changed.emit(f'情报网络构建失败：{message}')

    def _set_loading(self, loading: bool, message: str):
        self._refresh_btn.setEnabled(not loading)
        self._status_lbl.setText(message)
        self.bottom_status_changed.emit(message)

    def _render_current_pack(self):
        pack = self._current_pack
        if pack is None:
            return
        graph = dict(pack.graph_json)
        graph['meta'] = dict(graph.get('meta') or {})
        graph['meta']['performance_mode'] = self._perf_combo.currentText()
        self._graph_view.set_graph(graph)
        self._subject_lbl.setText(f'{pack.subject_code} {pack.subject_name}')
        self._meta_bar.setText(
            f'{pack.subject_name} | {len(pack.nodes)} 节点 / {len(pack.edges)} 关系 | '
            f'缺失证据：{len(pack.missing_layers)} | '
            f'{"WebEngine 3D" if self._graph_view.using_webengine else "2.5D 降级"}'
        )
        self._render_field_buttons(pack)
        self._render_evidence_overview(pack)

    def _render_evidence_overview(self, pack: EvidencePack):
        scores = pack.layer_scores
        rows = [
            f'<b>{pack.subject_code} {pack.subject_name}</b>',
            f'节点 {len(pack.nodes)} / 关系 {len(pack.edges)}',
        ]
        for layer, score in sorted(scores.items()):
            cnt = score.get('evidence_count', 0)
            missing = '，缺失' if score.get('missing') else ''
            rows.append(f'{layer}: {score.get("direction")} / {cnt} 条{missing}')
        self._evidence_box.setHtml('<br>'.join(rows))

    def _run_pack_analysis(self):
        if self._current_pack is None:
            self._analysis_box.setPlainText('当前没有可分析的 EvidencePack。')
            return
        analysis = analyze_evidence_pack('', self._current_pack)
        data = asdict(analysis)
        lines = [
            f'情报阶段：{data["intel_stage"]}',
            f'总体方向：{data["overall_direction"]} / 置信度 {data["confidence"]}/10',
            '主导变量：' + ('、'.join(data['dominant_variables'][:5]) or '暂无'),
            '利好证据链：' + ('；'.join(data['bullish_chain'][:3]) or '暂无'),
            '利空证据链：' + ('；'.join(data['bearish_chain'][:3]) or '暂无'),
            '缺失证据：' + ('、'.join(data['missing_evidence'][:5]) or '暂无'),
            '下一步观察：' + ('、'.join(data['next_watch'][:5]) or '暂无'),
            f'摘要：{data["ai_summary"]}',
        ]
        if data.get('forecast_hypotheses'):
            lines.append('预测假设：')
            for item in data['forecast_hypotheses'][:3]:
                lines.append(f'- {item["title"]}：{item["summary"]}')
        self._analysis_box.setPlainText('\n'.join(lines))

    def _focus_layers(self, layers: list[str], title: str):
        self._graph_view.focus_layers(layers)
        self._status_lbl.setText(f'聚焦：{title}')
        self.bottom_status_changed.emit(f'情报网络聚焦：{title}')

    def _on_performance_changed(self):
        self._render_current_pack()

    def _render_field_buttons(self, pack: EvidencePack):
        scores = pack.layer_scores
        for spec in self._field_button_specs:
            button = spec['button']
            layers = spec['layers']
            layer_scores = [scores[layer] for layer in layers if layer in scores]
            count = sum(int(score.get('evidence_count') or 0) for score in layer_scores)
            missing = sum(1 for score in layer_scores if score.get('missing'))
            if 'missing' in layers:
                missing += len(pack.missing_layers)
            directions = {str(score.get('direction') or 'unknown') for score in layer_scores}
            direction = _group_direction(directions)
            missing_text = f' · 缺失 {missing}' if missing else ''
            button.setText(f'{spec["title"]}\n{count} 条 · {direction}{missing_text}')


def _normalize_records(records: list[dict]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        code = _clean_code(rec.get('code') or '')
        if not code:
            continue
        group = str(rec.get('group') or '')
        key = (code, group)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            'code': code,
            'name': str(rec.get('name') or code),
            'group': group,
            'theme': str(rec.get('theme') or rec.get('note') or ''),
        })
    return out


def _clean_code(value: Any) -> str:
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _group_direction(directions: set[str]) -> str:
    if not directions:
        return '无证据'
    if 'mixed' in directions or ('bullish' in directions and 'bearish' in directions):
        return '分歧'
    if 'bearish' in directions:
        return '偏空'
    if 'bullish' in directions:
        return '偏多'
    if 'neutral' in directions:
        return '中性'
    return '未知'


def _field_group_color(layers: list[str]) -> str:
    for layer in layers:
        if layer in _FIELD_COLORS:
            return _FIELD_COLORS[layer]
    return CYAN


def _field_button_style(color: str) -> str:
    return f"""
QPushButton {{
    background-color: rgba(13, 27, 53, 0.68);
    color: {MUTED};
    border: 1px solid {BORDER};
    border-left: 3px solid {color};
    border-radius: 3px;
    padding: 5px 8px 5px 10px;
    font-size: 12px;
    text-align: left;
}}
QPushButton:hover {{
    background-color: #11284c;
    color: {TEXT};
    border-color: {BORDER_HI};
    border-left-color: {color};
}}
QPushButton:checked {{
    color: #f7fbff;
    background-color: #0b2a46;
    border-color: {color};
    border-left: 4px solid {color};
    font-weight: 600;
}}
QPushButton:disabled {{
    color: #596986;
    background-color: #0a1428;
    border-color: #172846;
}}
"""


def _text_box_style() -> str:
    return (
        f'QTextBrowser {{ background:{BG0}; color:{TEXT}; border:1px solid {BORDER}; '
        'border-radius:4px; padding:8px; font-size:12px; }}'
    )
