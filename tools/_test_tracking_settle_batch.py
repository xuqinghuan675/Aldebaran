import unittest
from datetime import date

from core.tracking import pending_settle_ids, try_settle_due_tasks


class PendingSettleIdsTest(unittest.TestCase):
    TODAY = date(2026, 6, 5)

    def test_open_past_deadline_is_pending(self):
        tasks = [{'id': '1', 'status': 'open', 'deadline': '2026-06-01'}]
        self.assertEqual(pending_settle_ids(tasks, self.TODAY), ['1'])

    def test_open_today_deadline_is_pending(self):
        tasks = [{'id': '1', 'status': 'open', 'deadline': '2026-06-05'}]
        self.assertEqual(pending_settle_ids(tasks, self.TODAY), ['1'])

    def test_open_future_deadline_not_pending(self):
        tasks = [{'id': '1', 'status': 'open', 'deadline': '2026-06-10'}]
        self.assertEqual(pending_settle_ids(tasks, self.TODAY), [])

    def test_closed_not_pending(self):
        tasks = [{'id': '1', 'status': 'closed', 'deadline': '2026-06-01'}]
        self.assertEqual(pending_settle_ids(tasks, self.TODAY), [])

    def test_no_deadline_not_pending(self):
        tasks = [{'id': '1', 'status': 'open', 'deadline': ''}]
        self.assertEqual(pending_settle_ids(tasks, self.TODAY), [])


class SettleOnlyIdsTest(unittest.TestCase):
    def _due_task(self):
        return {
            'id': '1', 'status': 'open', 'deadline': '2020-01-01',
            'code': '600519', 'name': 'x', 'direction': 'bullish',
            'entry_price': 100.0, 'task_class': 'buy',
        }

    def test_empty_only_ids_settles_nothing(self):
        newly = try_settle_due_tasks([self._due_task()], {}, only_ids=set())
        self.assertEqual(newly, [])

    def test_only_ids_excludes_others(self):
        newly = try_settle_due_tasks([self._due_task()], {}, only_ids={'other'})
        self.assertEqual(newly, [])


if __name__ == '__main__':
    unittest.main()
