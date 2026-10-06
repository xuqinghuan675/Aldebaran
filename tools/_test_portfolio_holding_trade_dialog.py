import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication, QMessageBox

from ui.portfolio_panel import _HoldingDialog


class PortfolioHoldingTradeDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_buy_mode_returns_weighted_cost_and_updated_shares(self):
        dlg = _HoldingDialog(
            prefill={
                'id': 'h_1',
                'code': '688313',
                'name': '仕佳光子',
                'shares': 100,
                'cost_price': 177.1,
            }
        )

        dlg._mode_tabs.setCurrentIndex(1)
        dlg._trade_shares.setValue(100)
        dlg._trade_price.setValue(200.0)
        dlg._on_ok()

        data = dlg.get_data()
        self.assertEqual(data['mode'], 'buy')
        self.assertEqual(data['shares'], 200)
        self.assertEqual(data['cost_price'], 188.55)
        self.assertEqual(data['trade_record']['direction'], 'buy')
        self.assertEqual(data['trade_record']['shares'], 100)
        self.assertEqual(data['trade_record']['price'], 200.0)
        self.assertFalse(hasattr(dlg, '_trade_date'))
        self.assertNotIn('date', data['trade_record'])

    def test_sell_mode_rejects_more_than_current_shares(self):
        dlg = _HoldingDialog(
            prefill={
                'id': 'h_1',
                'code': '688313',
                'name': '仕佳光子',
                'shares': 100,
                'cost_price': 177.1,
            }
        )

        dlg._mode_tabs.setCurrentIndex(2)
        dlg._trade_shares.setValue(200)
        dlg._trade_price.setValue(180.0)
        with mock.patch.object(QMessageBox, 'warning') as warning:
            dlg._on_ok()

        warning.assert_called_once()
        self.assertFalse(hasattr(dlg, '_result_data'))

    def test_sell_mode_reduces_shares_and_keeps_cost(self):
        dlg = _HoldingDialog(
            prefill={
                'id': 'h_1',
                'code': '688313',
                'name': '仕佳光子',
                'shares': 300,
                'cost_price': 177.1,
            }
        )

        dlg._mode_tabs.setCurrentIndex(2)
        dlg._trade_shares.setValue(100)
        dlg._trade_price.setValue(220.0)
        dlg._on_ok()

        data = dlg.get_data()
        self.assertEqual(data['mode'], 'sell')
        self.assertEqual(data['shares'], 200)
        self.assertEqual(data['cost_price'], 177.1)
        self.assertEqual(data['trade_record']['direction'], 'sell')
        self.assertEqual(data['trade_record']['price'], 220.0)


if __name__ == '__main__':
    unittest.main()
