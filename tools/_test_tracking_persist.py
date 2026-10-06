import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import tracking


class MergeSaveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._file = Path(self._tmp.name) / 'tracking_tasks.json'
        self._patch = patch.object(tracking, '_DATA_FILE', self._file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, tasks):
        self._file.write_text(json.dumps(tasks, ensure_ascii=False), encoding='utf-8')

    def _read_ids(self):
        return {t['id'] for t in json.loads(self._file.read_text(encoding='utf-8'))}

    def test_merge_preserves_tasks_added_by_other_writer(self):
        # worker 持旧快照（只有 A），落盘期间主线程已新建 B 到磁盘
        worker_snapshot = [{'id': 'A', 'status': 'closed', 'grade': 'B'}]
        self._write([{'id': 'A', 'status': 'open'}, {'id': 'B', 'status': 'open'}])
        tracking.save_tasks_merge(worker_snapshot)
        ids = self._read_ids()
        self.assertEqual(ids, {'A', 'B'})  # B 不能被旧快照抹掉

    def test_merge_writes_worker_version_for_owned_tasks(self):
        worker_snapshot = [{'id': 'A', 'status': 'closed', 'grade': 'A'}]
        self._write([{'id': 'A', 'status': 'open'}, {'id': 'B', 'status': 'open'}])
        tracking.save_tasks_merge(worker_snapshot)
        disk = {t['id']: t for t in json.loads(self._file.read_text(encoding='utf-8'))}
        self.assertEqual(disk['A']['status'], 'closed')  # worker 的处理结果生效
        self.assertEqual(disk['A']['grade'], 'A')
        self.assertEqual(disk['B']['status'], 'open')     # 他处任务原样保留

    def test_persist_task_fields_only_touches_one_task_on_fresh_disk(self):
        # 主线程旧列表里 A 还是 open，但磁盘上 worker 已把 A 结算为 closed
        self._write([
            {'id': 'A', 'status': 'closed', 'grade': 'A'},
            {'id': 'B', 'status': 'closed', 'grade': 'F'},
        ])
        updated = tracking.persist_task_fields('B', {'retrospective': '复盘文本'})
        self.assertEqual(updated['retrospective'], '复盘文本')
        disk = {t['id']: t for t in json.loads(self._file.read_text(encoding='utf-8'))}
        # A 的结算结果不能被回退
        self.assertEqual(disk['A']['status'], 'closed')
        self.assertEqual(disk['A']['grade'], 'A')
        self.assertEqual(disk['B']['retrospective'], '复盘文本')

    def test_persist_task_fields_missing_id_returns_none(self):
        self._write([{'id': 'A', 'status': 'open'}])
        self.assertIsNone(tracking.persist_task_fields('ZZZ', {'x': 1}))


if __name__ == '__main__':
    unittest.main()
