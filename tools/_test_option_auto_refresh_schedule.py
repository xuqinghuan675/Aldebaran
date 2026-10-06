import sys
import os
import unittest
from datetime import time as dt_time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QDialog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.stock_panel import (
    OPTION_AUTO_REFRESH_INTERVAL_MS,
    StockPanel,
    _OptionPositionDialog,
    is_option_auto_refresh_time,
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


class OptionAutoRefreshScheduleTest(unittest.TestCase):
    def test_new_option_dialog_defaults_contracts_to_one(self):
        _qt_app()
        dlg = _OptionPositionDialog()

        self.assertEqual(dlg._contracts_spin.minimum(), 1)
        self.assertEqual(dlg._contracts_spin.value(), 1)

    def test_option_edit_dialog_can_edit_pre_close(self):
        _qt_app()
        rec = {
            'code': '10011251',
            'name': '50ETF call Jul',
            'underlying_code': '510050',
            'option_type': 'call',
            'position_side': 'long_right',
            'strike_price': 2.65,
            'expiry_date': '2099-07-22',
            'contract_unit': 10000,
            'contracts': 2,
            'open_price': 0.25,
            'current_price': 0.3527,
            'pre_close': 0.296,
            'status': 'holding',
        }
        dlg = _OptionPositionDialog(rec=rec)

        self.assertAlmostEqual(dlg._pre_close_spin.value(), 0.296)
        dlg._pre_close_spin.setValue(0.3001)

        self.assertAlmostEqual(dlg.get_record()['pre_close'], 0.3001)

    def test_option_edit_rerenders_watchlist_totals_after_save(self):
        fake = _FakeOptionEditPanel()
        saved = []

        with patch('ui.stock_panel.load_option_watchlist', return_value=[{'code': '10011251', 'pre_close': 0.296}]), \
             patch('ui.stock_panel.save_option_watchlist', side_effect=lambda rows: saved.append(rows)), \
             patch('ui.stock_panel._OptionPositionDialog', _FakeAcceptedOptionDialog):
            StockPanel._option_edit(fake, 0)

        self.assertEqual(fake.option_table_refreshes, 1)
        self.assertEqual(fake.watchlist_rerenders, 1)
        self.assertAlmostEqual(saved[0][0]['pre_close'], 0.3001)

    def test_option_numeric_spinboxes_define_clickable_button_regions(self):
        _qt_app()
        dlg = _OptionPositionDialog()

        style = dlg._contracts_spin.styleSheet()
        self.assertIn('QSpinBox::up-button', style)
        self.assertIn('QSpinBox::down-button', style)

    def test_fixed_option_auto_refresh_interval_is_8_seconds(self):
        self.assertEqual(OPTION_AUTO_REFRESH_INTERVAL_MS, 8000)

    def test_option_auto_refresh_time_windows_are_inclusive_by_minute(self):
        inside = [
            dt_time(9, 25),
            dt_time(11, 35, 59),
            dt_time(12, 55),
            dt_time(15, 5, 59),
        ]
        outside = [
            dt_time(9, 24, 59),
            dt_time(11, 36),
            dt_time(12, 54, 59),
            dt_time(15, 6),
        ]

        for value in inside:
            self.assertTrue(is_option_auto_refresh_time(value), value)
        for value in outside:
            self.assertFalse(is_option_auto_refresh_time(value), value)

    def test_option_timer_tick_refreshes_only_inside_option_window(self):
        fake = _FakeStockPanel(active=True)

        with patch('ui.stock_panel.is_option_auto_refresh_time', return_value=True):
            StockPanel._option_auto_refresh_tick(fake)

        self.assertEqual(fake.calls, [{'silent': True, 'force': True}])

    def test_option_timer_tick_skips_outside_option_window(self):
        fake = _FakeStockPanel(active=True)

        with patch('ui.stock_panel.is_option_auto_refresh_time', return_value=False):
            StockPanel._option_auto_refresh_tick(fake)

        self.assertEqual(fake.calls, [])

    def test_option_tab_entry_forces_immediate_refresh(self):
        fake = _FakeStockPanel(active=True)

        StockPanel._refresh_option_on_entry(fake)

        self.assertEqual(fake.calls, [{'silent': True, 'force': True}])

    def test_stock_panel_refresh_forces_active_option_tab_refresh(self):
        fake = _FakeStockPanel(active=True)
        fake._ipo_widget = _FakeRefreshable()

        with patch('ui.stock_panel.QTimer.singleShot', side_effect=lambda _ms, cb: cb()):
            StockPanel.refresh(fake)

        self.assertEqual(fake.calls, [{'silent': True, 'force': True}])

    def test_manual_option_refresh_reports_existing_refresh(self):
        fake = _FakeOptionRefreshPanel()

        with patch('ui.stock_panel.QMessageBox.information') as info:
            StockPanel._option_refresh_quotes(fake, silent=False)

        info.assert_called_once()

    def test_option_quote_refresh_keeps_contracts_edited_while_worker_was_running(self):
        fake = _FakeOptionEditPanel()
        stale_records = [{
            'code': '10011251',
            'contracts': 1,
            'current_price': 0.25,
            'pre_close': 0.24,
            'status': 'holding',
            'expiry_date': '2099-07-22',
        }]
        current_records = [{
            'code': '10011251',
            'contracts': 3,
            'current_price': 0.25,
            'pre_close': 0.24,
            'status': 'holding',
            'expiry_date': '2099-07-22',
        }]
        quotes = {
            '10011251': {
                'ok': True,
                'current_price': 0.31,
                'pre_close': 0.29,
                'expiry_date': '2099-07-22',
            },
        }
        saved = []

        with patch('ui.stock_panel.load_option_watchlist', return_value=current_records), \
             patch('ui.stock_panel.save_option_watchlist', side_effect=lambda rows: saved.append(rows)):
            StockPanel._on_option_quotes_ready(
                fake,
                quotes,
                [(0, stale_records[0])],
                stale_records,
                True,
                None,
            )

        self.assertEqual(saved[0][0]['contracts'], 3)
        self.assertAlmostEqual(saved[0][0]['current_price'], 0.31)

    def test_option_quote_refresh_does_not_restore_record_deleted_while_worker_was_running(self):
        fake = _FakeOptionEditPanel()
        stale_records = [
            {
                'code': '10011251',
                'contracts': 1,
                'current_price': 0.25,
                'pre_close': 0.24,
                'status': 'holding',
                'expiry_date': '2099-07-22',
            },
            {
                'code': '10011252',
                'contracts': 2,
                'current_price': 0.35,
                'pre_close': 0.34,
                'status': 'holding',
                'expiry_date': '2099-07-22',
            },
        ]
        current_records = [dict(stale_records[1])]
        quotes = {
            '10011251': {
                'ok': True,
                'current_price': 0.31,
                'pre_close': 0.29,
                'expiry_date': '2099-07-22',
            },
            '10011252': {
                'ok': True,
                'current_price': 0.36,
                'pre_close': 0.34,
                'expiry_date': '2099-07-22',
            },
        }
        saved = []

        with patch('ui.stock_panel.load_option_watchlist', return_value=current_records), \
             patch('ui.stock_panel.save_option_watchlist', side_effect=lambda rows: saved.append(rows)):
            StockPanel._on_option_quotes_ready(
                fake,
                quotes,
                list(enumerate(stale_records)),
                stale_records,
                True,
                None,
            )

        self.assertEqual([row['code'] for row in saved[0]], ['10011252'])
        self.assertAlmostEqual(saved[0][0]['current_price'], 0.36)


class _FakeStockPanel:
    def __init__(self, active):
        self.active = active
        self.calls = []

    def _is_option_tab_active(self):
        return self.active

    def _option_refresh_quotes(self, **kwargs):
        self.calls.append(kwargs)

    def _refresh_option_on_entry(self):
        StockPanel._refresh_option_on_entry(self)

    def _refresh_etf_update_btn_visible(self):
        pass

    def _fetch(self):
        pass

    def _fetch_watchlist(self):
        pass


class _FakeRefreshable:
    def refresh(self):
        pass


class _FakeRunningWorker:
    def isRunning(self):
        return True


class _FakeOptionRefreshPanel:
    _option_worker = _FakeRunningWorker()

    def sender(self):
        return None


class _FakeOptionEditPanel:
    def __init__(self):
        self._last_watchlist_snap = {'rows': []}
        self.option_table_refreshes = 0
        self.watchlist_rerenders = 0

    def _refresh_option_table(self):
        self.option_table_refreshes += 1

    def _rerender_watchlist(self):
        self.watchlist_rerenders += 1

    def _refresh_option_views(self):
        StockPanel._refresh_option_views(self)


class _FakeAcceptedOptionDialog:
    def __init__(self, _parent, rec):
        self._rec = rec

    def exec(self):
        return QDialog.DialogCode.Accepted

    def get_record(self):
        updated = dict(self._rec)
        updated['pre_close'] = 0.3001
        return updated


if __name__ == '__main__':
    unittest.main()
