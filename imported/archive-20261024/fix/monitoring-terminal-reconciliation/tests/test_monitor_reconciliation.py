"""監視済みターンと最新の観測値を照合する回帰テスト。"""
import pathlib
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
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
    def test_new_quota_failure_can_be_armed_later(self):
        reconcile_monitoring(self.db,[dict(id=TID,title='デモ',cwd='',limited=True,failure_turn='new-failure')])
        row=list_threads(self.db)[0]
        self.assertEqual(row['state'],'idle')
        self.assertTrue(row['enabled'])
    def test_non_quota_completion_requires_review(self):
        reconcile_monitoring(self.db,[dict(id=TID,title='デモ',cwd='',limited=False,failure_turn=None)])
        row=list_threads(self.db)[0]
        self.assertEqual(row['state'],'blocked')
        self.assertFalse(row['enabled'])
    def test_same_old_failure_must_not_rearm(self):
        reconcile_monitoring(self.db,[dict(id=TID,title='デモ',cwd='',limited=True,failure_turn='old-failure')])
        self.assertEqual(list_threads(self.db)[0]['state'],'monitoring')
if __name__=='__main__':
    unittest.main()
