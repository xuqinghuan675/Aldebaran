import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.user_data_io as user_data_io


REQUIRED_USER_KEYS = {
    'watchlist',
    'watchlist_groups',
    'option_watchlist',
    'position_watchlist',
    'pnl_calendar',
    'holdings_pnl_calendar',
}


class UserDataIoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / 'aldebaran_data'
        self.cache = self.home / 'cache'
        self.mem_dir = self.home / 'data' / 'analysis_memory'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.mem_dir.mkdir(parents=True, exist_ok=True)

        self._orig_home = user_data_io._HOME
        self._orig_cache = user_data_io._CACHE
        self._orig_mem_dir = user_data_io._MEM_DIR
        self._orig_user_files = dict(user_data_io._USER_FILES)
        self._orig_text_user_files = dict(user_data_io._TEXT_USER_FILES)
        self._orig_reset_only_files = list(getattr(user_data_io, '_RESET_ONLY_FILES', []))
        self._orig_reset_only_globs = list(getattr(user_data_io, '_RESET_ONLY_GLOBS', []))

    def tearDown(self):
        user_data_io._HOME = self._orig_home
        user_data_io._CACHE = self._orig_cache
        user_data_io._MEM_DIR = self._orig_mem_dir
        user_data_io._USER_FILES = self._orig_user_files
        user_data_io._TEXT_USER_FILES = self._orig_text_user_files
        user_data_io._RESET_ONLY_FILES = self._orig_reset_only_files
        user_data_io._RESET_ONLY_GLOBS = self._orig_reset_only_globs
        self._tmp.cleanup()

    def _remap_module_paths(self):
        cache_file_names = {
            'watchlist.json',
            'watchlist_groups.json',
            'option_watchlist.json',
            'ai_predictions.json',
            'pnl_calendar.json',
            'holdings_pnl_calendar.json',
        }
        user_data_io._HOME = self.home
        user_data_io._CACHE = self.cache
        user_data_io._MEM_DIR = self.mem_dir
        user_data_io._USER_FILES = {
            key: (self.cache / path.name if path.name in cache_file_names else self.home / path.name)
            for key, path in user_data_io._USER_FILES.items()
        }
        user_data_io._TEXT_USER_FILES = {
            key: self.home / path.name
            for key, path in user_data_io._TEXT_USER_FILES.items()
        }
        user_data_io._RESET_ONLY_FILES = [
            self.cache / path.name
            for path in getattr(user_data_io, '_RESET_ONLY_FILES', [])
        ]
        user_data_io._RESET_ONLY_GLOBS = [
            self.cache / path.name
            for path in getattr(user_data_io, '_RESET_ONLY_GLOBS', [])
        ]

    def test_user_data_manifest_includes_options_and_self_selected_files(self):
        self.assertTrue(
            REQUIRED_USER_KEYS.issubset(user_data_io._USER_FILES),
            f'missing user data keys: {sorted(REQUIRED_USER_KEYS - set(user_data_io._USER_FILES))}',
        )
        self.assertEqual(user_data_io._USER_FILES['option_watchlist'].name, 'option_watchlist.json')
        self.assertEqual(user_data_io._USER_FILES['watchlist_groups'].name, 'watchlist_groups.json')
        self.assertEqual(user_data_io._USER_FILES['position_watchlist'].name, 'position_watchlist.json')

    def test_export_import_and_reset_cover_options_and_watchlist_state(self):
        self.assertTrue(REQUIRED_USER_KEYS.issubset(user_data_io._USER_FILES))
        self._remap_module_paths()

        seed_data = {
            'watchlist': [{'code': '600519', 'name': 'stock'}],
            'watchlist_groups': ['client'],
            'option_watchlist': [{'code': '10000001', 'name': 'option', 'contracts': 1}],
            'position_watchlist': [{'code': '000001', 'name': 'position-watch'}],
            'pnl_calendar': [{'date': '2026-06-18', 'total_pnl': 120.0, 'stock_count': 2}],
            'holdings_pnl_calendar': [{'date': '2026-06-18', 'total_pnl': 39451.0, 'stock_count': 215}],
        }
        for key, data in seed_data.items():
            path = user_data_io._USER_FILES[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding='utf-8')

        derived_files = [
            self.cache / 'watchlist_data.json',
            self.cache / 'watchlist_fund_600519.json',
        ]
        for path in derived_files:
            path.write_text('{}', encoding='utf-8')

        bundle_path = self.home / 'bundle.json'
        status = user_data_io.export_user_data(bundle_path)

        self.assertEqual(status.get('_write'), 'ok')
        bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
        for key, data in seed_data.items():
            self.assertEqual(bundle['data'][key], data)

        for key in seed_data:
            user_data_io._USER_FILES[key].write_text('[]', encoding='utf-8')
        result = user_data_io.import_user_data(bundle_path)

        self.assertTrue(result.success, result.errors)
        for key, data in seed_data.items():
            restored = json.loads(user_data_io._USER_FILES[key].read_text(encoding='utf-8'))
            self.assertEqual(restored, data)

        user_data_io.reset_user_data()

        for key in seed_data:
            self.assertFalse(user_data_io._USER_FILES[key].exists(), key)
        for path in derived_files:
            self.assertFalse(path.exists(), path.name)

    def test_import_merges_lists_and_dicts_instead_of_replacing_all_existing_data(self):
        self._remap_module_paths()

        existing_data = {
            'watchlist': [
                {'code': '600519', 'group': 'A', 'name': 'old-stock'},
                {'code': '000001', 'group': 'A', 'name': 'local-stock'},
            ],
            'watchlist_groups': ['A', 'local'],
            'option_watchlist': [
                {'code': '10000001', 'name': 'old-option', 'contracts': 1},
                {'code': '10000002', 'name': 'local-option', 'contracts': 1},
            ],
            'holdings': [
                {'id': 'h1', 'code': '600519', 'name': 'old-holding', 'shares': 100},
                {'id': 'h2', 'code': '000001', 'name': 'local-holding', 'shares': 100},
            ],
            'position_watchlist': [
                {'code': '300750', 'name': 'local-position-watch'},
            ],
            'tracking_tasks': [
                {'id': 't1', 'code': '600519', 'name': 'old-task'},
                {'id': 't2', 'code': '000001', 'name': 'local-task'},
            ],
            'user_profile': {
                'risk': 'old',
                'local_only': True,
            },
            'holdings_pnl_calendar': [
                {'date': '2026-06-18', 'total_pnl': 100.0},
                {'date': '2026-06-19', 'total_pnl': 200.0},
            ],
        }
        import_data = {
            'watchlist': [
                {'code': '600519', 'group': 'A', 'name': 'imported-stock'},
                {'code': '002415', 'group': 'B', 'name': 'new-stock'},
            ],
            'watchlist_groups': ['A', 'imported'],
            'option_watchlist': [
                {'code': '10000001', 'name': 'imported-option', 'contracts': 2},
                {'code': '10000003', 'name': 'new-option', 'contracts': 1},
            ],
            'holdings': [
                {'id': 'h9', 'code': '600519', 'name': 'imported-holding', 'shares': 200},
                {'id': 'h3', 'code': '002415', 'name': 'new-holding', 'shares': 100},
            ],
            'position_watchlist': [
                {'code': '300750', 'name': 'imported-position-watch'},
                {'code': '002415', 'name': 'new-position-watch'},
            ],
            'tracking_tasks': [
                {'id': 't1', 'code': '600519', 'name': 'imported-task'},
                {'id': 't3', 'code': '002415', 'name': 'new-task'},
            ],
            'user_profile': {
                'risk': 'imported',
            },
            'holdings_pnl_calendar': [
                {'date': '2026-06-18', 'total_pnl': 300.0},
                {'date': '2026-06-20', 'total_pnl': 400.0},
            ],
        }

        for key, data in existing_data.items():
            path = user_data_io._USER_FILES[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding='utf-8')
        event_path = user_data_io._TEXT_USER_FILES['tracking_events']
        event_path.write_text(
            '{"id":"e1","message":"old-event"}\n'
            '{"id":"e2","message":"local-event"}\n',
            encoding='utf-8',
        )
        import_data['tracking_events'] = (
            '{"id":"e1","message":"imported-event"}\n'
            '{"id":"e3","message":"new-event"}\n'
        )

        bundle_path = self.home / 'merge_bundle.json'
        bundle_path.write_text(
            json.dumps({'version': user_data_io._VERSION, 'data': import_data}),
            encoding='utf-8',
        )

        result = user_data_io.import_user_data(bundle_path)

        self.assertTrue(result.success, result.errors)
        watchlist = json.loads(user_data_io._USER_FILES['watchlist'].read_text(encoding='utf-8'))
        self.assertEqual([r['name'] for r in watchlist], ['imported-stock', 'local-stock', 'new-stock'])

        groups = json.loads(user_data_io._USER_FILES['watchlist_groups'].read_text(encoding='utf-8'))
        self.assertEqual(groups, ['A', 'local', 'imported'])

        options = json.loads(user_data_io._USER_FILES['option_watchlist'].read_text(encoding='utf-8'))
        self.assertEqual([r['name'] for r in options], ['imported-option', 'local-option', 'new-option'])

        holdings = json.loads(user_data_io._USER_FILES['holdings'].read_text(encoding='utf-8'))
        self.assertEqual([r['name'] for r in holdings], ['imported-holding', 'local-holding', 'new-holding'])

        position_watch = json.loads(user_data_io._USER_FILES['position_watchlist'].read_text(encoding='utf-8'))
        self.assertEqual([r['name'] for r in position_watch], ['imported-position-watch', 'new-position-watch'])

        tasks = json.loads(user_data_io._USER_FILES['tracking_tasks'].read_text(encoding='utf-8'))
        self.assertEqual([r['name'] for r in tasks], ['imported-task', 'local-task', 'new-task'])

        profile = json.loads(user_data_io._USER_FILES['user_profile'].read_text(encoding='utf-8'))
        self.assertEqual(profile, {'risk': 'imported', 'local_only': True})

        holdings_calendar = json.loads(user_data_io._USER_FILES['holdings_pnl_calendar'].read_text(encoding='utf-8'))
        self.assertEqual(
            [(r['date'], r['total_pnl']) for r in holdings_calendar],
            [('2026-06-18', 300.0), ('2026-06-19', 200.0), ('2026-06-20', 400.0)],
        )

        events = [
            json.loads(line)
            for line in user_data_io._TEXT_USER_FILES['tracking_events'].read_text(encoding='utf-8').splitlines()
        ]
        self.assertEqual([e['message'] for e in events], ['imported-event', 'local-event', 'new-event'])

    def test_import_accepts_v2_bundle_with_compatible_data_schema(self):
        self._remap_module_paths()

        existing = [{'code': '000001', 'group': 'A', 'name': 'local-stock'}]
        incoming = [{'code': '600519', 'group': 'A', 'name': 'v2-stock'}]
        watchlist_path = user_data_io._USER_FILES['watchlist']
        watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        watchlist_path.write_text(json.dumps(existing), encoding='utf-8')

        bundle_path = self.home / 'v2_bundle.json'
        bundle_path.write_text(
            json.dumps({'version': 'v2.0', 'data': {'watchlist': incoming}}),
            encoding='utf-8',
        )

        result = user_data_io.import_user_data(bundle_path)

        self.assertTrue(result.success, result.errors)
        restored = json.loads(watchlist_path.read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in restored], ['local-stock', 'v2-stock'])

    def test_import_accepts_any_version_and_skips_incompatible_fields(self):
        self._remap_module_paths()

        watchlist_path = user_data_io._USER_FILES['watchlist']
        groups_path = user_data_io._USER_FILES['watchlist_groups']
        profile_path = user_data_io._USER_FILES['user_profile']
        for path, payload in (
            (watchlist_path, [{'code': '000001', 'group': 'A', 'name': 'local-stock'}]),
            (groups_path, ['local']),
            (profile_path, {'risk': 'local'}),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding='utf-8')

        bundle_path = self.home / 'future_bundle.json'
        bundle_path.write_text(
            json.dumps({
                'version': 'future-v99',
                'data': {
                    'watchlist': [
                        {'name': 'missing-code'},
                        'bad-row',
                        {'code': '600519', 'group': 'A', 'name': 'future-stock'},
                    ],
                    'watchlist_groups': {'bad': 'type'},
                    'user_profile': ['bad-type'],
                    'future_only_key': {'new': True},
                },
            }),
            encoding='utf-8',
        )

        result = user_data_io.import_user_data(bundle_path)

        self.assertTrue(result.success, result.errors)
        self.assertIn('watchlist_groups', result.skipped)
        self.assertIn('user_profile', result.skipped)
        self.assertIn('watchlist: 2 incompatible item(s)', result.skipped)
        watchlist = json.loads(watchlist_path.read_text(encoding='utf-8'))
        self.assertEqual([item['name'] for item in watchlist], ['local-stock', 'future-stock'])
        self.assertEqual(json.loads(groups_path.read_text(encoding='utf-8')), ['local'])
        self.assertEqual(json.loads(profile_path.read_text(encoding='utf-8')), {'risk': 'local'})


if __name__ == '__main__':
    unittest.main()
