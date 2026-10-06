import json
import re
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import tracking


class LauncherAndPackagingTest(unittest.TestCase):
    def test_launch_bat_is_ascii_and_directory_relative(self):
        data = (ROOT / 'launch.bat').read_bytes()
        self.assertTrue(all(b < 128 for b in data), 'launch.bat must be ASCII-only')
        text = data.decode('ascii')
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIsNone(re.search(r'(?i)\\b[A-Z]:\\\\', text))

    def test_shortcut_script_is_ascii_and_targets_launcher(self):
        data = (ROOT / 'tools' / 'make_shortcut.ps1').read_bytes()
        self.assertTrue(all(b < 128 for b in data), 'make_shortcut.ps1 must be ASCII-only')
        text = data.decode('ascii')
        self.assertIn('$PSScriptRoot', text)
        self.assertIn('launch.bat', text)
        self.assertIsNone(re.search(r'(?i)\\b[A-Z]:\\\\', text))
        self.assertNotIn('pythonw.exe', text)

    def test_build_script_refuses_unpinned_qt_runtime(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        req = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('PySide6==6.10.2', req)
        self.assertNotIn('PyQt6', req)
        self.assertIn('$RequiredPySide = "6.10.2"', build)
        self.assertIn('$RequiredQtPrefix = "6.10."', build)
        self.assertIn('Refusing to build with this Qt runtime', build)
        self.assertIn('from PySide6.QtCore import __version__ as PYSIDE_VERSION_STR, qVersion', build)
        self.assertIn('"--enable-plugin=pyside6"', build)
        self.assertIn('"--include-module=PySide6.QtNetwork"', build)
        self.assertIn('[switch]$PreflightOnly', build)
        self.assertIn('Build preflight passed', build)

    def test_periodic_report_table_dependency_is_packaged_when_available(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        req = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
        self.assertIn('pdfplumber>=0.11', req)
        self.assertIn("import pdfplumber", build)
        self.assertIn("pdfplumber missing; table extraction disabled", build)
        self.assertIn('"--include-package=pdfplumber"', build)
        self.assertIn('$PdfplumberAvailable', build)
        self.assertIn('+ $optionalPackageArgs + @(', build)
        self.assertNotIn(',\n        $optionalPackageArgs,\n', build)

    def test_matplotlib_qtagg_binding_is_forced_to_pyside6(self):
        for rel in (
            'ui/market_panel.py',
            'ui/portfolio_panel.py',
            'ui/sector_panel.py',
            'ui/stock_panel.py',
        ):
            source = (ROOT / rel).read_text(encoding='utf-8')
            self.assertIn('from core.qt_runtime import configure_qt_runtime', source, rel)
            self.assertIn('configure_qt_runtime()', source, rel)
            self.assertLess(
                source.index('configure_qt_runtime()'),
                source.index('matplotlib.backends.backend_qtagg'),
                rel,
            )

    def test_pyside_import_hook_is_compatible_with_six_moves_imports(self):
        code = (
            "from core.qt_runtime import configure_qt_runtime\n"
            "configure_qt_runtime()\n"
            "from PySide6.QtCore import Qt\n"
            "import matplotlib\n"
            "matplotlib.use('QtAgg')\n"
            "from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg\n"
            "import akshare\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, '-c', code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_script_refuses_parallel_or_stale_nuitka_builds(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('build.lock', build)
        self.assertIn('FileShare]::None', build)
        self.assertIn('Get-CimInstance Win32_Process', build)
        self.assertIn('ProcessId -ne $PID', build)
        self.assertIn('nuitka|Backend\\.scons|app\\.build|app\\.dist', build)
        self.assertIn('another Aldebaran build process is active', build)

    def test_build_script_fails_fast_for_missing_resources_and_outputs(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('Assert-FileExists', build)
        self.assertIn('Missing required build resource', build)
        self.assertIn('Missing build output', build)
        self.assertIn('Missing release output', build)
        self.assertIn('ZIP creation failed', build)
        self.assertNotIn(
            'Move-Item "$DIST\\Aldebaran.exe" "$REL\\" -Force -ErrorAction SilentlyContinue',
            build,
        )

    def test_cloud_desktop_launcher_uses_standalone_mode(self):
        data = (ROOT / 'tools' / 'build_nuitka_standalone.bat').read_bytes()
        self.assertTrue(all(b < 128 for b in data), 'cloud launcher must be ASCII-only')
        text = data.decode('ascii')
        self.assertIn('cd /d "%~dp0.."', text)
        self.assertIn('build.ps1" -Mode standalone', text)

    def test_build_script_labels_and_assembles_standalone_release(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('$buildTitle = if ($Mode -eq', build)
        self.assertIn('Nuitka Standalone Build', build)
        self.assertIn('Aldebaran_V3.0_云桌面版', build)
        self.assertIn('$distDir = "$DIST\\app.dist"', build)
        self.assertIn('Assert-DirectoryExists $distDir "Missing build output"', build)
        self.assertIn('Assert-FileExists "$distDir\\Aldebaran.exe" "Missing build output"', build)


class TrackingAtomicSaveTest(unittest.TestCase):
    def test_save_tasks_preserves_existing_file_if_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_file = Path(tmp) / 'tracking_tasks.json'
            original = [{'id': 'old', 'status': 'open'}]
            data_file.write_text(json.dumps(original), encoding='utf-8')
            with patch.object(tracking, '_DATA_FILE', data_file):
                with patch.object(Path, 'replace', side_effect=OSError('replace failed')):
                    tracking.save_tasks([{'id': 'new', 'status': 'closed'}])
            disk = json.loads(data_file.read_text(encoding='utf-8'))
            self.assertEqual(disk, original)


class QtThreadDispatchTest(unittest.TestCase):
    def test_monitor_thread_uses_qt_signals_not_qtimer_from_worker_thread(self):
        source = (ROOT / 'ui' / 'main_window.py').read_text(encoding='utf-8')
        self.assertIn('_portfolio_prices_ready = Signal(dict)', source)
        self.assertIn('_tracking_prices_ready = Signal(dict)', source)
        self.assertIn('self._portfolio_prices_ready.emit(real_prices)', source)
        self.assertIn('self._tracking_prices_ready.emit(track_prices)', source)
        self.assertNotIn('lambda p=real_prices: pp.set_current_prices(p)', source)
        self.assertNotIn('lambda p=track_prices: tp.set_current_prices(p)', source)

    def test_window_close_forces_full_process_exit(self):
        source = (ROOT / 'ui' / 'main_window.py').read_text(encoding='utf-8')
        self.assertIn('def closeEvent(self, event):', source)
        self.assertIn('self.timer.stop()', source)
        self.assertIn('self.market_timer.stop()', source)
        self.assertIn('self._tray.hide()', source)
        self.assertIn('QApplication.instance().quit()', source)
        self.assertIn('os._exit(0)', source)


if __name__ == '__main__':
    unittest.main()
