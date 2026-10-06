import unittest
from pathlib import Path
import sys

import requests
import requests.sessions as request_sessions

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import http_client


class HttpClientTest(unittest.TestCase):
    def setUp(self):
        self._orig_request = request_sessions.Session.request
        self.calls = []

        def fake_request(session, method, url, **kwargs):
            self.calls.append({
                'session': session,
                'method': method,
                'url': url,
                'kwargs': kwargs,
            })
            return object()

        request_sessions.Session.request = fake_request

    def tearDown(self):
        request_sessions.Session.request = self._orig_request

    def test_default_timeout_is_injected(self):
        http_client.get('https://example.test/quotes')

        self.assertEqual(
            self.calls[0]['kwargs']['timeout'],
            http_client.DEFAULT_TIMEOUT,
        )

    def test_explicit_timeout_is_preserved(self):
        http_client.get('https://example.test/quotes', timeout=3)

        self.assertEqual(self.calls[0]['kwargs']['timeout'], 3)

    def test_default_user_agent_is_injected(self):
        http_client.get('https://example.test/quotes')

        self.assertEqual(
            self.calls[0]['kwargs']['headers']['User-Agent'],
            http_client.DEFAULT_USER_AGENT,
        )

    def test_caller_headers_can_override_default_user_agent(self):
        http_client.get(
            'https://example.test/quotes',
            headers={'User-Agent': 'custom-agent', 'Accept': 'application/json'},
        )

        headers = self.calls[0]['kwargs']['headers']
        self.assertEqual(headers['User-Agent'], 'custom-agent')
        self.assertEqual(headers['Accept'], 'application/json')

    def test_request_uses_default_trust_env(self):
        http_client.get('https://example.test/quotes')

        self.assertIs(self.calls[0]['session'].trust_env, http_client.DEFAULT_TRUST_ENV)

    def test_session_has_default_headers_and_trust_env(self):
        s = http_client.session()

        self.assertEqual(s.headers['User-Agent'], http_client.DEFAULT_USER_AGENT)
        self.assertIs(s.trust_env, http_client.DEFAULT_TRUST_ENV)

    def test_session_headers_can_override_default_user_agent(self):
        s = http_client.session(headers={'User-Agent': 'session-agent'})

        self.assertEqual(s.headers['User-Agent'], 'session-agent')

    def test_request_get_post_share_default_parameters(self):
        http_client.request('PATCH', 'https://example.test/item')
        http_client.get('https://example.test/item')
        http_client.post('https://example.test/item', json={'x': 1})

        self.assertEqual([c['method'] for c in self.calls], ['PATCH', 'GET', 'POST'])
        for call in self.calls:
            self.assertEqual(call['kwargs']['timeout'], http_client.DEFAULT_TIMEOUT)
            self.assertEqual(
                call['kwargs']['headers']['User-Agent'],
                http_client.DEFAULT_USER_AGENT,
            )
        self.assertEqual(self.calls[2]['kwargs']['json'], {'x': 1})

    def test_request_failure_is_logged_and_raised(self):
        def failing_request(session, method, url, **kwargs):
            raise requests.Timeout('slow')

        request_sessions.Session.request = failing_request

        with self.assertLogs('core.http_client', level='DEBUG') as logs:
            with self.assertRaises(requests.Timeout):
                http_client.get('https://example.test/slow')

        self.assertIn('HTTP GET https://example.test/slow failed', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
