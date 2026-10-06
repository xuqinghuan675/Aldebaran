import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class BuildResourceHardeningTest(unittest.TestCase):
    def test_build_preflight_covers_clean_data_preparation(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        preflight_pos = build.index('if ($PreflightOnly)')
        prepare_pos = build.index('Preparing data files')
        compile_pos = build.index('Nuitka compiling')

        self.assertGreater(preflight_pos, prepare_pos)
        self.assertLess(preflight_pos, compile_pos)

    def test_clean_build_seeds_empty_intel_config_without_local_secret_file(self):
        example = ROOT / 'data' / 'intel_config.example.json'
        self.assertTrue(example.exists(), 'tracked example config must exist')
        self.assertEqual(example.read_text(encoding='utf-8').strip(), '{}')

        ignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('!data/intel_config.example.json', ignore)

        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('$intelConfigDest = "$CLEAN_DATA\\intel_config.json"', build)
        self.assertIn('$safeIntelConfig = @{}', build)
        self.assertIn("$safeIntelConfig['enable_overseas']", build)
        self.assertIn('ConvertTo-Json -Compress', build)
        for secret_name in ('deepseek_api_key', 'github_token', 'tickflow_token', 'tickflow_free_token'):
            self.assertNotIn(f"$safeIntelConfig['{secret_name}']", build)
        self.assertNotIn('"intel_config.json") | ForEach-Object', build)


    def test_build_packages_graph_assets_notices_and_uses_portable_parallelism(self):
        build = (ROOT / 'tools' / 'build.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('"--include-data-dir=$APP_ROOT\\ui\\assets=ui/assets"', build)
        self.assertIn('Copy-Item -LiteralPath "$APP_ROOT\\LICENSE"', build)
        self.assertIn('Copy-Item -LiteralPath "$APP_ROOT\\THIRD_PARTY_NOTICES.md"', build)
        self.assertIn('$BuildJobs = [Environment]::ProcessorCount', build)
        self.assertIn('ALDEBARAN_BUILD_JOBS', build)
        self.assertIn('"--jobs=$BuildJobs"', build)
        self.assertNotIn('"--jobs=32"', build)


class MainWindowHardeningTest(unittest.TestCase):
    def test_stock_panel_load_failure_uses_existing_statusbar_and_warning(self):
        source = (ROOT / 'ui' / 'main_window.py').read_text(encoding='utf-8')
        self.assertNotIn('self.status_update.emit', source)
        self.assertIn('self.statusBar().showMessage', source)
        self.assertIn('QMessageBox.warning', source)


class JsRuntimeHardeningTest(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('ALDEBARAN_PY_MINI_RACER_STUBBED', None)

    def test_stub_marker_is_detectable_from_runtime_helper(self):
        from core.js_runtime import is_py_mini_racer_stubbed

        stub = types.ModuleType('py_mini_racer')
        stub.__aldebaran_stubbed__ = True
        with patch.dict(sys.modules, {'py_mini_racer': stub}):
            self.assertTrue(is_py_mini_racer_stubbed())

    def test_app_stub_sets_module_and_environment_markers(self):
        source = (ROOT / 'app.py').read_text(encoding='utf-8')
        self.assertIn('__aldebaran_stubbed__', source)
        self.assertIn('ALDEBARAN_PY_MINI_RACER_STUBBED', source)


class CredentialHardeningTest(unittest.TestCase):
    def test_save_api_key_returns_false_and_logs_write_failure(self):
        from core import credentials

        config_path = ROOT / 'this_path_should_not_be_written.json'
        with patch.object(credentials, '_CONFIG_FILE', config_path):
            with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
                with self.assertLogs('core.credentials', level='WARNING') as logs:
                    ok = credentials.save_api_key('sk-test')

        self.assertFalse(ok)
        self.assertIn('DeepSeek API Key 保存失败', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
