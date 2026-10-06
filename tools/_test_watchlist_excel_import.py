import sys
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from openpyxl import Workbook
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QTableWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.stock_panel import (
    StockPanel,
    _WATCHLIST_HEADERS,
    _default_wl_shares,
    _is_valid_stock_code,
)


_QT_APP = None


def _qt_app():
    global _QT_APP
    app = QApplication.instance()
    if app is None:
        _QT_APP = QApplication([])
        return _QT_APP
    _QT_APP = app
    return app


def _normalize_like_import(raw_code: str) -> str | None:
    c = str(raw_code).strip().upper()
    for suf in ('.SS', '.SH', '.SZ', '.CSI'):
        if c.endswith(suf):
            c = c[:-len(suf)]
            break
    if c.startswith(('SH', 'SZ', 'BJ')):
        c = c[2:]
    if not c.isdigit():
        return None
    if len(c) == 5:
        c = '0' + c
    if len(c) != 6:
        return None
    return c if _is_valid_stock_code(c) else None


class WatchlistExcelImportTest(unittest.TestCase):
    def _make_watchlist_panel_harness(self):
        _qt_app()
        panel = StockPanel.__new__(StockPanel)
        panel._wl_default_group = '默认'
        panel._wl_active_group = None
        panel._wl_sort_col = None
        panel._wl_sort_desc = True
        panel.table_watchlist = QTableWidget(0, len(_WATCHLIST_HEADERS))
        panel._wl_total_label = QLabel()
        panel._update_wl_total_label = lambda _rows: None
        return panel

    def test_watchlist_checked_rows_survive_table_rerender(self):
        panel = self._make_watchlist_panel_harness()
        rows = [
            {'code': '515880', 'name': '证券ETF', 'group': 'DMX', 'price': 1.0, 'pct': 0.0},
            {'code': '512890', 'name': '红利低波', 'group': 'DMX', 'price': 2.0, 'pct': 0.0},
        ]

        panel._fill_watchlist_table(rows)
        panel.table_watchlist.item(0, 0).setCheckState(Qt.CheckState.Checked)

        panel._fill_watchlist_table(list(reversed(rows)))

        checked_by_code = {
            panel.table_watchlist.item(r, 0).text(): panel.table_watchlist.item(r, 0).checkState()
            for r in range(panel.table_watchlist.rowCount())
        }
        self.assertEqual(checked_by_code['515880'], Qt.CheckState.Checked)
        self.assertEqual(checked_by_code['512890'], Qt.CheckState.Unchecked)

    def test_xlsm_parser_reads_etf_code_column_from_non_active_sheet(self):
        wb = Workbook()
        active = wb.active
        active.title = '行情数据表'
        active.append(['日期', '511010_国泰国债ETF'])
        active.append(['2026-06-15', 141.0])

        ws = wb.create_sheet('ETF列表')
        ws.append(['ETF代码', 'ETF名称', '交易所'])
        ws.append([511010, '国泰国债ETF', 'SH'])
        ws.append(['159915.SZ', '创业板ETF', 'SZ'])
        ws.append(['510300', '沪深300ETF', 'SH'])

        path = Path(tempfile.gettempdir()) / f'_test_watchlist_import_{id(self)}.xlsm'
        wb.save(path)
        try:
            codes, names = StockPanel._parse_watchlist_xlsx(str(path), _normalize_like_import)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(codes, ['511010', '159915', '510300'])
        self.assertEqual(names['511010'], '国泰国债ETF')
        self.assertEqual(names['159915'], '创业板ETF')

    def test_import_records_skip_duplicates_and_use_current_price_defaults(self):
        records = StockPanel._build_watchlist_import_records(
            ['600519', '000001', '600519', '300750'],
            {'600519': '贵州茅台', '300750': '宁德时代'},
            {'000001'},
            {
                '600519': {'price': 1600.0, 'name': '贵州茅台'},
                '300750': {'price': 200.0, 'name': '宁德时代'},
            },
            group='当前分组',
            today='2026-06-15',
        )

        self.assertEqual([r['code'] for r in records], ['600519', '300750'])
        self.assertEqual(records[0]['shares'], _default_wl_shares(1600.0))
        self.assertEqual(records[0]['cost_price'], 1600.0)
        self.assertEqual(records[0]['add_price'], 1600.0)
        self.assertEqual(records[0]['add_date'], '2026-06-15')
        self.assertEqual(records[0]['group'], '当前分组')
        self.assertEqual(records[1]['shares'], _default_wl_shares(200.0))


if __name__ == '__main__':
    unittest.main()
