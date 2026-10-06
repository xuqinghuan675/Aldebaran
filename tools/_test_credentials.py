import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import credential_store
from core import credentials


_EMPTY_ENV = {
    'DEEPSEEK_API_KEY': '',
    'GITHUB_TOKEN': '',
    'TICKFLOW_TOKEN': '',
    'TICKFLOW_FREE_TOKEN': '',
}


class CredentialJsonModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / 'intel_config.json'
        self._config_patch = patch.object(credentials, '_CONFIG_FILE', self.config_path)
        self._env_patch = patch.dict(os.environ, _EMPTY_ENV, clear=False)
        self._config_patch.start()
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._config_patch.stop()
        self._tmp.cleanup()

    def test_json_mode_roundtrip_uses_existing_flat_schema(self):
        self.assertTrue(credentials.save_api_key(' sk-deepseek '))
        self.assertTrue(credentials.save_github_token(' ghp-token '))
        self.assertTrue(credentials.save_tickflow_token(' paid-tickflow '))
        self.assertTrue(credentials.save_tickflow_free_token(' free-tickflow '))

        cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['deepseek_api_key'], 'sk-deepseek')
        self.assertEqual(cfg['github_token'], 'ghp-token')
        self.assertEqual(cfg['tickflow_token'], 'paid-tickflow')
        self.assertEqual(cfg['tickflow_free_token'], 'free-tickflow')
        self.assertNotIn('credential_store_mode', cfg)

        self.assertEqual(credentials.load_api_key(), 'sk-deepseek')
        self.assertEqual(credentials.load_github_token(), 'ghp-token')
        self.assertEqual(credentials.load_tickflow_token(), 'paid-tickflow')
        self.assertEqual(credentials.load_tickflow_free_token(), 'free-tickflow')

    def test_environment_variables_keep_highest_priority(self):
        self.config_path.write_text(
            json.dumps(
                {
                    'deepseek_api_key': 'file-deepseek',
                    'github_token': 'file-github',
                    'tickflow_token': 'file-paid',
                    'tickflow_free_token': 'file-free',
                }
            ),
            encoding='utf-8',
        )

        with patch.dict(
            os.environ,
            {
                'DEEPSEEK_API_KEY': 'env-deepseek',
                'GITHUB_TOKEN': 'env-github',
                'TICKFLOW_TOKEN': 'env-paid',
                'TICKFLOW_FREE_TOKEN': 'env-free',
            },
            clear=False,
        ):
            self.assertEqual(credentials.load_api_key(), 'env-deepseek')
            self.assertEqual(credentials.load_github_token(), 'env-github')
            self.assertEqual(credentials.load_tickflow_token(), 'env-paid')
            self.assertEqual(credentials.load_tickflow_free_token(), 'env-free')


class CredentialSecureModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tmp.name) / 'intel_config.json'
        self._config_patch = patch.object(credentials, '_CONFIG_FILE', self.config_path)
        self._env_patch = patch.dict(os.environ, _EMPTY_ENV, clear=False)
        self._config_patch.start()
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._config_patch.stop()
        self._tmp.cleanup()

    def test_refuses_secure_mode_on_non_windows_without_rewriting_plain_file(self):
        self.config_path.write_text(
            json.dumps({'deepseek_api_key': 'plain-secret'}),
            encoding='utf-8',
        )

        with self.assertLogs('core.credential_store', level='WARNING'):
            with patch.object(credential_store, '_is_windows', return_value=False):
                result = credential_store.enable_secure_mode(self.config_path)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'dpapi_unavailable')
        self.assertIn('Windows DPAPI', result.error)
        cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['deepseek_api_key'], 'plain-secret')
        self.assertNotIn('credential_store_mode', cfg)

    def test_refuses_secure_mode_when_dpapi_encrypt_is_unavailable(self):
        self.config_path.write_text(
            json.dumps({'github_token': 'ghp-plain'}),
            encoding='utf-8',
        )

        with self.assertLogs('core.credential_store', level='WARNING'):
            with patch.object(credential_store, '_is_windows', return_value=True):
                with patch.object(
                    credential_store,
                    '_protect_secret',
                    side_effect=credential_store.CredentialStoreError(
                        'dpapi_unavailable',
                        'CryptProtectData unavailable',
                    ),
                ):
                    result = credential_store.enable_secure_mode(self.config_path)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'dpapi_unavailable')
        cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['github_token'], 'ghp-plain')
        self.assertNotIn('credential_store_mode', cfg)

    def test_refuses_secure_mode_when_dpapi_probe_fails_for_empty_config(self):
        with self.assertLogs('core.credential_store', level='WARNING'):
            with patch.object(credential_store, '_is_windows', return_value=True):
                with patch.object(
                    credential_store,
                    '_protect_secret',
                    side_effect=credential_store.CredentialStoreError(
                        'dpapi_unavailable',
                        'CryptProtectData unavailable',
                    ),
                ):
                    result = credential_store.enable_secure_mode(self.config_path)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'dpapi_unavailable')
        self.assertFalse(self.config_path.exists())

    def test_loads_dpapi_ciphertext(self):
        payload = base64.b64encode(b'cipher-bytes').decode('ascii')
        self.config_path.write_text(
            json.dumps(
                {
                    'credential_store_mode': 'dpapi',
                    'deepseek_api_key': f'dpapi:{payload}',
                }
            ),
            encoding='utf-8',
        )

        with patch.object(
            credential_store,
            '_unprotect_secret',
            return_value='sk-secure',
        ) as unprotect:
            self.assertEqual(credentials.load_api_key(), 'sk-secure')

        unprotect.assert_called_once_with(b'cipher-bytes')

    def test_secure_save_does_not_fallback_to_plaintext_when_dpapi_unavailable(self):
        self.config_path.write_text(
            json.dumps({'credential_store_mode': 'dpapi'}),
            encoding='utf-8',
        )

        with self.assertLogs(level='WARNING'):
            with patch.object(credential_store, '_is_windows', return_value=True):
                with patch.object(
                    credential_store,
                    '_protect_secret',
                    side_effect=credential_store.CredentialStoreError(
                        'dpapi_unavailable',
                        'CryptProtectData unavailable',
                    ),
                ):
                    ok = credentials.save_api_key('sk-should-not-be-plain')

        self.assertFalse(ok)
        cfg = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertNotIn('deepseek_api_key', cfg)

    def test_save_failure_returns_false(self):
        with self.assertLogs(level='WARNING'):
            with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
                ok = credentials.save_github_token('ghp-test')

        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
