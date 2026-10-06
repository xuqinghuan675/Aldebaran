import json
import tempfile
import unittest
from pathlib import Path

import core.app_settings as s


class AppSettingsTest(unittest.TestCase):
    def setUp(self):
        self._orig = s._FILE
        s._FILE = Path(tempfile.gettempdir()) / f'_test_app_settings_{id(self)}.json'

    def tearDown(self):
        if s._FILE.exists():
            s._FILE.unlink()
        s._FILE = self._orig

    def test_default_when_missing(self):
        self.assertEqual(s.load_setting('x', 'def'), 'def')

    def test_roundtrip_bool(self):
        s.save_setting('auto_settle', False)
        self.assertEqual(s.load_setting('auto_settle', True), False)

    def test_preserves_other_keys(self):
        s.save_setting('a', 1)
        s.save_setting('b', 2)
        self.assertEqual(s.load_setting('a'), 1)
        self.assertEqual(s.load_setting('b'), 2)

    def test_does_not_clobber_existing_file(self):
        s._FILE.write_text(json.dumps({'deepseek_api_key': 'sk-x'}), encoding='utf-8')
        s.save_setting('auto_settle', False)
        self.assertEqual(s.load_setting('deepseek_api_key'), 'sk-x')


if __name__ == '__main__':
    unittest.main()
