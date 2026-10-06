import unittest
from pathlib import Path
import sys
from unittest.mock import patch

import requests.sessions as request_sessions

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.net_setup import setup_requests_defaults


class RequestsDefaultsTest(unittest.TestCase):
    def setUp(self):
        self._orig_init = request_sessions.Session.__init__
        self._orig_request = request_sessions.Session.request
        if hasattr(request_sessions.Session, '_aldebaran_defaults_patched'):
            delattr(request_sessions.Session, '_aldebaran_defaults_patched')

    def tearDown(self):
        request_sessions.Session.__init__ = self._orig_init
        request_sessions.Session.request = self._orig_request
        if hasattr(request_sessions.Session, '_aldebaran_defaults_patched'):
            delattr(request_sessions.Session, '_aldebaran_defaults_patched')

    def test_injects_default_timeout_when_request_omits_timeout(self):
        calls = []

        def fake_request(self, method, url, **kwargs):
            calls.append(kwargs)
            return object()

        request_sessions.Session.request = fake_request

        setup_requests_defaults()
        request_sessions.Session().get('https://example.test/quotes')

        self.assertEqual(calls[0]['timeout'], 10)

    def test_preserves_explicit_timeout(self):
        calls = []

        def fake_request(self, method, url, **kwargs):
            calls.append(kwargs)
            return object()

        request_sessions.Session.request = fake_request

        setup_requests_defaults()
        request_sessions.Session().get('https://example.test/quotes', timeout=3)

        self.assertEqual(calls[0]['timeout'], 3)

    def test_can_disable_global_patch_with_environment_switch(self):
        calls = []

        def fake_request(self, method, url, **kwargs):
            calls.append(kwargs)
            return object()

        request_sessions.Session.request = fake_request

        with patch.dict('os.environ', {'ALDEBARAN_REQUESTS_DEFAULTS': '0'}):
            setup_requests_defaults()
        request_sessions.Session().get('https://example.test/quotes')

        self.assertNotIn('timeout', calls[0])

    def test_default_keeps_global_patch_enabled(self):
        calls = []

        def fake_request(self, method, url, **kwargs):
            calls.append(kwargs)
            return object()

        request_sessions.Session.request = fake_request

        with patch.dict('os.environ', {}, clear=True):
            setup_requests_defaults()
        request_sessions.Session().get('https://example.test/quotes')

        self.assertEqual(calls[0]['timeout'], 10)


if __name__ == '__main__':
    unittest.main()
