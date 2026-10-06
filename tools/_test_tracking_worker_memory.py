import types
import unittest
from unittest.mock import patch

import ui.tracking_panel as panel_mod


class _Signal:
    def connect(self, _callback):
        pass


class _FakeSettlementWorker:
    created = []

    def __init__(self, task_ids, prices, parent=None):
        self.task_ids = set(task_ids)
        self.prices = prices
        self.parent = parent
        self.completed = _Signal()
        self.failed = _Signal()
        self.finished = _Signal()
        self.started = False
        _FakeSettlementWorker.created.append(self)

    def isRunning(self):
        return False

    def start(self):
        self.started = True


class SettlementWorkerMemoryTest(unittest.TestCase):
    def setUp(self):
        _FakeSettlementWorker.created.clear()

    def test_settle_next_batch_does_not_deepcopy_full_task_tree(self):
        large_payload = {'pred_snapshot': {'blob': 'x' * 1000}}
        tasks = [
            {
                'id': 'due-1',
                'status': 'open',
                'kind': 'stock',
                'code': '600000',
                **large_payload,
            },
        ]
        panel = types.SimpleNamespace(
            _settlement_worker=None,
            _convert_cooldown={},
            _current_prices={'600000': {'price': 10.0}},
            _initial_sweep_done=False,
            _tasks=tasks,
            _settle_attempted=set(),
            _on_settlement_done=lambda _result: None,
            _on_settlement_error=lambda _error: None,
        )

        def fail_on_deepcopy(_obj):
            raise AssertionError('full task tree should not be deep-copied')

        with patch.object(panel_mod, 'pending_maintenance_ids', return_value=['due-1']), \
             patch.object(panel_mod, '_is_convertible_watch', return_value=False), \
             patch.object(panel_mod, '_SettlementWorker', _FakeSettlementWorker), \
             patch.object(panel_mod, 'deepcopy', side_effect=fail_on_deepcopy):
            panel_mod.TrackingPanel._settle_next_batch(panel)

        self.assertEqual(len(_FakeSettlementWorker.created), 1)
        worker = _FakeSettlementWorker.created[0]
        self.assertEqual(worker.task_ids, {'due-1'})
        self.assertTrue(worker.started)
        self.assertIs(panel._settlement_worker, worker)


if __name__ == '__main__':
    unittest.main()
