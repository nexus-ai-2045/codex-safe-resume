"""監視中ターンの照合で、利用枠以外の終了を自動再開しないことを検証する。合成DBのみ。"""
import pathlib
import tempfile
import unittest

from core import connection, sync, enable, list_threads
from watch import reconcile_monitoring

TID='123e4567-e89b-12d3-a456-426614174000'


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connection(pathlib.Path(self.temp.name)/'state')
        sync(self.db,[dict(id=TID,title='デモ',cwd='',limited=False,failure_turn=None)])
        enable(self.db,TID,True)
        self.db.execute("UPDATE threads SET state='monitoring',failure_turn='old-failure' WHERE id=?",(TID,))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_non_quota_completion_requires_review(self):
        reconcile_monitoring(self.db,[dict(id=TID,title='デモ',cwd='',limited=False,failure_turn=None)])
        row=list_threads(self.db)[0]
        self.assertEqual(row['state'],'blocked')
        self.assertFalse(row['enabled'])


if __name__=='__main__':
    unittest.main()
