import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RuntimePathsTest(unittest.TestCase):
    def tearDown(self):
        import core.paths as paths
        importlib.reload(paths)

    def test_development_default_uses_dot_aldebaran(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {}, clear=False), \
                patch.dict(os.environ, {
                    'ALDEBARAN_DATA_DIR': '',
                    'ALDEBARAN_INTELLIGENCE_CACHE_DIR': '',
                }, clear=False), \
                patch.object(Path, 'home', return_value=Path(tmp)), \
                patch.object(sys, 'frozen', False, create=True):
            paths = self._reload_paths()
            data_root = paths.app_data_root()
            cache = paths.cache_root()
            intel_cache = paths.intelligence_cache_root()

        self.assertEqual(data_root, Path(tmp) / '.aldebaran')
        self.assertEqual(cache, Path(tmp) / '.aldebaran' / 'cache')
        self.assertEqual(intel_cache, Path(tmp) / '.aldebaran' / 'cache' / 'intelligence')

    def test_aldebaran_data_dir_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'ALDEBARAN_DATA_DIR': tmp}, clear=False), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(Path(tmp) / 'bin' / 'Aldebaran.exe')):
            paths = self._reload_paths()
            data_root = paths.app_data_root()
            cache = paths.cache_root()

        self.assertEqual(data_root, Path(tmp))
        self.assertEqual(cache, Path(tmp) / 'cache')

    def test_intelligence_cache_dir_override_is_preserved(self):
        with tempfile.TemporaryDirectory() as data_tmp, tempfile.TemporaryDirectory() as intel_tmp, \
                patch.dict(os.environ, {
                    'ALDEBARAN_DATA_DIR': data_tmp,
                    'ALDEBARAN_INTELLIGENCE_CACHE_DIR': intel_tmp,
                }, clear=False):
            paths = self._reload_paths()
            data_root = paths.app_data_root()
            intel_cache = paths.intelligence_cache_root()

        self.assertEqual(data_root, Path(data_tmp))
        self.assertEqual(intel_cache, Path(intel_tmp))

    def test_frozen_runtime_uses_exe_sibling_aldebaran_data(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {
                    'ALDEBARAN_DATA_DIR': '',
                    'ALDEBARAN_INTELLIGENCE_CACHE_DIR': '',
                    'NUITKA_ONEFILE_BINARY': '',
                }, clear=False), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'argv', [str(Path(tmp) / 'Aldebaran.exe')]), \
                patch.object(sys, 'executable', str(Path(tmp) / 'Aldebaran.exe')):
            paths = self._reload_paths()
            data_root = paths.app_data_root()

        self.assertEqual(data_root, Path(tmp) / 'AldebaranData')

    def test_onefile_prefers_original_argv0_over_extraction_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_exe = root / 'release' / 'Aldebaran.exe'
            extracted_exe = root / 'cache' / 'v3_0_1' / 'Aldebaran.exe'
            with patch.dict(os.environ, {
                    'ALDEBARAN_DATA_DIR': '',
                    'ALDEBARAN_INTELLIGENCE_CACHE_DIR': '',
                    'NUITKA_ONEFILE_BINARY': str(extracted_exe),
                }, clear=False), \
                    patch.object(sys, 'argv', [str(release_exe)]), \
                    patch.object(sys, 'executable', str(extracted_exe)):
                paths = self._reload_paths()
                data_root = paths.app_data_root()

        self.assertEqual(data_root, release_exe.parent / 'AldebaranData')

    def test_ensure_app_data_dirs_creates_runtime_tree(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {
                    'ALDEBARAN_DATA_DIR': tmp,
                    'ALDEBARAN_INTELLIGENCE_CACHE_DIR': '',
                    'NUITKA_ONEFILE_BINARY': '',
                }, clear=False), \
                patch.object(sys, 'frozen', False, create=True):
            paths = self._reload_paths()
            paths.ensure_app_data_dirs()
            root = Path(tmp)
            documents_root = paths.documents_cache_root()

            expected = [
                root / 'cache' / 'intelligence' / 'documents' / 'disclosures' / 'announcements',
                root / 'cache' / 'intelligence' / 'documents' / 'disclosures' / 'periodic_reports',
                root / 'cache' / 'intelligence' / 'documents' / 'extracted_text' / 'announcements',
                root / 'cache' / 'intelligence' / 'documents' / 'extracted_text' / 'periodic_reports',
                root / 'cache' / 'intelligence' / 'documents' / 'metadata',
                root / 'logs',
                root / 'config',
            ]
            for path in expected:
                self.assertTrue(path.is_dir(), str(path))
            self.assertEqual(documents_root, root / 'cache' / 'intelligence' / 'documents')

    def _reload_paths(self):
        import core.paths as paths
        return importlib.reload(paths)


if __name__ == '__main__':
    unittest.main()
