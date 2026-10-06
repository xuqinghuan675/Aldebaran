import sys
import os
import traceback
import types

# 打包 exe：提前注入 py_mini_racer 空壳，防止 akshare 加载 V8 DLL 崩溃。
# 其他包（scipy/pyarrow/curl_cffi 等）由 Nuitka 正常编译打入，无需空壳。
if 'py_mini_racer' not in sys.modules:
    try:
        import py_mini_racer as _pmr  # noqa: F401
        # 源码环境直接使用真实包。
    except (ImportError, ModuleNotFoundError):
        _pmr_stub = types.ModuleType('py_mini_racer')
        _pmr_stub.__aldebaran_stubbed__ = True
        os.environ['ALDEBARAN_PY_MINI_RACER_STUBBED'] = '1'
        class _MiniRacer:
            """空壳：不抛异常，返回空数据。akshare 某些页面会尝试 JS 渲染，
            若 V8 DLL 不可用则降级为空（调用方一般有非 JS 兜底解析）。"""
            def eval(self, *a, **kw):
                return ''
        _pmr_stub.MiniRacer = _MiniRacer
        sys.modules['py_mini_racer'] = _pmr_stub

# pythonw.exe 没有控制台，stdout/stderr 为 None，需要重定向避免崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

from core.qt_runtime import configure_qt_runtime
configure_qt_runtime()

# 在导入任何依赖 requests 的模块前，注入默认 Chrome UA。
# 不动系统代理：requests 的 trust_env 保持默认，系统代理正常生效。
from core.net_setup import setup_requests_defaults
setup_requests_defaults()

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QIcon

from core.constants import STYLESHEET
from ui.main_window import MainWindow


from core.paths import LOG_FILE, ensure_data_dir

# 应用图标（绝对路径，避免 cwd 不同导致找不到；只读资源，跟随 __file__ 安全）
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_icon.jpg')
# 崩溃日志写到用户目录，避免 Nuitka onefile 临时目录被清理后日志丢失
LOG_PATH = str(LOG_FILE)


def _write_crash_log(exc_type, exc_value, exc_tb):
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write('\n' + '=' * 80 + '\n')
        f.write('Aldebaran crash\n')
        traceback.print_exception(exc_type, exc_value, exc_tb, file=f)


def _set_windows_taskbar_id():
    """Windows: pythonw.exe 启动时任务栏图标默认走 Python 解释器，
    通过 AppUserModelID 隔离，让任务栏显示我们自己的图标。"""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'aldebaran.desktop.v3'
        )
    except Exception:
        pass


# ---------- 启动 ----------
if __name__ == '__main__':
    ensure_data_dir()  # 首次启动创建 ~/.aldebaran/data/ + 迁移旧数据
    sys.excepthook = _write_crash_log
    _set_windows_taskbar_id()

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    app.setFont(QFont("Microsoft YaHei", 10))

    # 单实例（a+c）：已在运行则通知已有实例激活窗口、自己退出，不起新进程
    # （窗口慢出来时用户反复双击尤其云桌面会挤爆资源）。基于命名管道，
    # 上一个实例崩溃后管道消失，新实例能正常接管，不会永久锁死。
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    _SI_NAME = 'aldebaran_v3_single_instance'
    _probe = QLocalSocket()
    _probe.connectToServer(_SI_NAME)
    if _probe.waitForConnected(200):
        _probe.write(b'show')
        _probe.flush()
        _probe.waitForBytesWritten(300)
        sys.exit(0)
    _probe.abort()
    QLocalServer.removeServer(_SI_NAME)
    _si_server = QLocalServer()
    _si_server.listen(_SI_NAME)

    if os.path.exists(ICON_PATH):
        app.setWindowIcon(QIcon(ICON_PATH))

    try:
        window = MainWindow()

        def _activate_existing():
            from PySide6.QtCore import Qt
            while _si_server.hasPendingConnections():
                _si_server.nextPendingConnection().readAll()
            window.setWindowState(
                (window.windowState() & ~Qt.WindowState.WindowMinimized)
                | Qt.WindowState.WindowActive
            )
            window.show()
            window.raise_()
            window.activateWindow()
        _si_server.newConnection.connect(_activate_existing)

        window.show()
        sys.exit(app.exec())
    except Exception:
        _write_crash_log(*sys.exc_info())
        raise
