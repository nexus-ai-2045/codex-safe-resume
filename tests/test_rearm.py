"""両枠と受領ターンを照合した繰り返し再開（模擬bridge）。"""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core import connection,sync,enable,quota,list_threads
from watch import once
T='123e4567-e89b-12d3-a456-426614174000'
NOW=1800000000
class FakeBridge:
    calls=[]
    latest='failure'
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def capabilities(self): return {'safe_admission':True,'desktop_binding':True}
    def quota(self): return {'rateLimits':{'primary':{'usedPercent':0},'secondary':{'usedPercent':0}}}
    def snapshot(self,tid):
        return dict(id=T,title='デモ',cwd='',limited=True,failure_turn=self.latest,
                    latest_turn=self.latest,turn_status='failed',awaiting=False,observation_verified=True)
    def send_once(self,thread_id,expected_turn, *, attempt_id=None):
        self.calls.append((thread_id,expected_turn))
        return {'turn':{'id':'new-'+expected_turn}}
class Integration(unittest.TestCase):
    def test_rearm_across_two_quota_windows(self):
        FakeBridge.calls=[]; FakeBridge.latest='failure'
        with tempfile.TemporaryDirectory() as td, closing(connection(Path(td)/'state')) as db:
            bridge=FakeBridge(); a=bridge.snapshot(T)
            sync(db,[a]);enable(db,T,True);quota(db,T,'failure',NOW-1)
            with patch('core.time.time',return_value=NOW):
                for failure in ('failure','new-failure'):
                    FakeBridge.latest=failure
                    with patch('watch.discover',return_value=([bridge.snapshot(T)],None)):
                        result=once(db,FakeBridge)
                    self.assertEqual(result['results'],[(T,'dispatched')])
                    self.assertEqual(list_threads(db)[0]['resume_turn'],'new-'+failure)
            self.assertEqual(FakeBridge.calls,[(T,'failure'),(T,'new-failure')])
if __name__=='__main__': unittest.main()
