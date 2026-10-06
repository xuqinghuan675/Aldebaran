import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QHBoxLayout, QWidget

from ui.main_window import MainWindow


class MainWindowWatermarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_installs_small_watermark_in_toolbar(self):
        win = MainWindow.__new__(MainWindow)
        toolbar_host = QWidget()
        toolbar = QHBoxLayout(toolbar_host)

        MainWindow._install_watermark(win, toolbar)

        self.assertIsInstance(win._watermark, QLabel)
        self.assertIs(win._watermark.parentWidget(), toolbar_host)
        self.assertEqual('Aldebaran', win._watermark.text())
        self.assertTrue(
            win._watermark.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        )
        self.assertIn('font-size: 22px', win._watermark.styleSheet())
        self.assertLessEqual(win._watermark.maximumHeight(), 34)
        self.assertEqual(toolbar.itemAt(toolbar.count() - 1).widget(), win._watermark)


if __name__ == '__main__':
    unittest.main()
