"""監視中ターンの照合で、利用枠以外の終了を自動再開しないことを検証する。合成DBのみ。"""
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from core import connection, sync, enable, list_threads
from watch import reconcile_monitoring

TID='123e4567-e89b-12d3-a456-426614174000'
RECEIPT='resumed-turn'


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connection(pathlib.Path(self.temp.name)/'state')
        sync(self.db,[dict(id=TID,title='デモ',cwd='',limited=False,failure_turn=None)])
        enable(self.db,TID,True)
        self.db.execute("UPDATE threads SET state='monitoring',failure_turn='old-failure',resume_turn=? WHERE id=?",(RECEIPT,TID))

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reconcile(self, status):
        observation=dict(id=TID,title='デモ',cwd='',limited=False,failure_turn=None,
                         latest_turn=RECEIPT,turn_status=status,awaiting=False,
                         observation_verified=True)
        with patch('watch._bridge',side_effect=AssertionError('送信経路を開いてはいけません')) as bridge:
            reconcile_monitoring(self.db,[observation])
            bridge.assert_not_called()
        return list_threads(self.db)[0]

    def assert_requires_review(self, status):
        row=self.reconcile(status)
        self.assertEqual(row['resume_turn'],RECEIPT)
        self.assertEqual(row['state'],'blocked')
        self.assertFalse(row['enabled'])
        self.assertEqual(row['last_error'],'終了状態の確認が必要です')

    def test_non_quota_failed_requires_review(self):
        self.assert_requires_review('failed')

    def test_interrupted_requires_review(self):
        self.assert_requires_review('interrupted')

    def test_verified_completed_turn_finishes_monitoring(self):
        row=self.reconcile('completed')
        self.assertEqual(row['resume_turn'],RECEIPT)
        self.assertEqual(row['state'],'completed')
        self.assertFalse(row['enabled'])
        self.assertEqual(row['last_error'],'')

    def test_missing_receipt_blocks_before_completion(self):
        self.db.execute('UPDATE threads SET resume_turn=NULL WHERE id=?',(TID,))
        row=self.reconcile('completed')
        self.assertEqual(row['state'],'blocked')
        self.assertFalse(row['enabled'])
        self.assertEqual(row['last_error'],'再開ターンを確認できません')


if __name__=='__main__':
    unittest.main()
