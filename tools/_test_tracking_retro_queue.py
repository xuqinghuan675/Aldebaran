from datetime import datetime, timedelta
import unittest

from ui.tracking_retro_scheduler import take_auto_retro_batch


def _closed_task(task_id: str, minutes_ago: int = 1) -> dict:
    return {
        'id': task_id,
        'status': 'closed',
        'grade': 'A',
        'retrospective': None,
        'closed_at': (datetime(2026, 6, 5, 15, 0) - timedelta(minutes=minutes_ago)).isoformat(timespec='seconds'),
    }


class AutoRetrospectiveBatchTest(unittest.TestCase):
    def test_auto_retrospective_batch_respects_available_slots(self):
        tasks = [_closed_task(str(i)) for i in range(10)]

        batch = take_auto_retro_batch(
            tasks,
            started_ids=set(),
            running_count=1,
            max_concurrent=3,
            now=datetime(2026, 6, 5, 15, 0),
        )

        self.assertEqual([task['id'] for task in batch], ['0', '1'])

    def test_auto_retrospective_batch_skips_started_tasks(self):
        tasks = [_closed_task(str(i)) for i in range(4)]

        batch = take_auto_retro_batch(
            tasks,
            started_ids={'0', '2'},
            running_count=0,
            max_concurrent=3,
            now=datetime(2026, 6, 5, 15, 0),
        )

        self.assertEqual([task['id'] for task in batch], ['1', '3'])


if __name__ == '__main__':
    unittest.main()
