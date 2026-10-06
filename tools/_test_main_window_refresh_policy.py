import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.main_window import MainWindow


class MainWindowRefreshPolicyTest(unittest.TestCase):
    def test_all_nav_pages_refresh_when_switched_to(self):
        win = MainWindow.__new__(MainWindow)

        for idx in range(8):
            self.assertTrue(MainWindow._should_refresh_on_switch(win, idx), idx)


if __name__ == '__main__':
    unittest.main()
