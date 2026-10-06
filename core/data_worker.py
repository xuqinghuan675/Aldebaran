import time
from datetime import datetime

from PySide6.QtCore import QThread, Signal


# ---------- 后台数据线程 ----------
class DataWorker(QThread):
    """通用数据获取线程。默认抓取行业板块即时资金流，3次重试指数退避。"""
    data_ready = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, fetcher=None, parent=None):
        super().__init__(parent)
        self._fetcher = fetcher

    def run(self):
        last_error = None
        for attempt in range(3):
            try:
                if self._fetcher is None:
                    raise RuntimeError('未指定数据获取函数')
                df = self._fetcher()
                self.data_ready.emit(df)
                return
            except Exception as e:
                last_error = e
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s
        self.error_occurred.emit(f"重试3次仍失败: {last_error}")


def is_trading_time(dt=None):
    if dt is None:
        dt = datetime.now()
    if dt.weekday() >= 5:
        return False
    hm = dt.hour * 100 + dt.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def is_lunch_break(dt=None):
    """11:30 < t < 13:00 视为午休（工作日）"""
    if dt is None:
        dt = datetime.now()
    if dt.weekday() >= 5:
        return False
    hm = dt.hour * 100 + dt.minute
    return 1130 < hm < 1300
