import unittest

from ui.tracking_retro_scheduler import pending_retro_tasks


class PendingRetroTest(unittest.TestCase):
    def test_closed_graded_without_retro_is_pending(self):
        tasks = [{'id': '1', 'status': 'closed', 'grade': 'A', 'retrospective': None}]
        self.assertEqual([t['id'] for t in pending_retro_tasks(tasks)], ['1'])

    def test_open_task_not_pending(self):
        tasks = [{'id': '1', 'status': 'open', 'grade': None, 'retrospective': None}]
        self.assertEqual(pending_retro_tasks(tasks), [])

    def test_already_has_retro_not_pending(self):
        tasks = [{'id': '1', 'status': 'closed', 'grade': 'A', 'retrospective': '已复盘'}]
        self.assertEqual(pending_retro_tasks(tasks), [])

    def test_closed_without_grade_not_pending(self):
        tasks = [{'id': '1', 'status': 'closed', 'grade': None, 'retrospective': None}]
        self.assertEqual(pending_retro_tasks(tasks), [])

    def test_counts_multiple_in_order(self):
        tasks = [
            {'id': '1', 'status': 'closed', 'grade': 'A', 'retrospective': None},
            {'id': '2', 'status': 'closed', 'grade': 'F', 'retrospective': ''},
            {'id': '3', 'status': 'closed', 'grade': 'B', 'retrospective': 'done'},
            {'id': '4', 'status': 'open', 'grade': None},
        ]
        self.assertEqual([t['id'] for t in pending_retro_tasks(tasks)], ['1', '2'])


if __name__ == '__main__':
    unittest.main()
