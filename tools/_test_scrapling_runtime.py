import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ScraplingRuntimeTest(unittest.TestCase):
    def test_missing_external_runtime_returns_controlled_error(self):
        from core.intelligence.scrapling_runtime import run_scrapling_fetch

        result = run_scrapling_fetch(
            'https://example.com',
            runtime_python='Z:\\missing\\python.exe',
        )

        self.assertFalse(result['ok'])
        self.assertEqual(result['mode'], 'static')
        self.assertIn('runtime not found', result['error'])

    def test_runner_parses_probe_json_without_importing_scrapling(self):
        from core.intelligence.scrapling_runtime import run_scrapling_fetch

        payload = {
            'ok': True,
            'url': 'https://example.com',
            'mode': 'static',
            'title': 'Example Domain',
            'text': 'Example Domain text',
            'items': [],
            'error': '',
        }

        with patch('core.intelligence.scrapling_runtime.subprocess.run') as run:
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(payload),
                stderr='',
            )

            result = run_scrapling_fetch(
                'https://example.com',
                runtime_python=sys.executable,
                timeout_seconds=3,
            )

        self.assertTrue(result['ok'])
        self.assertEqual(result['title'], 'Example Domain')
        command = run.call_args.args[0]
        self.assertIn('--json', command)
        self.assertIn('--mode', command)
        self.assertIn('static', command)


if __name__ == '__main__':
    unittest.main()
