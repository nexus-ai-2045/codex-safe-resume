from contextlib import closing
import pathlib,sys,unittest,tempfile
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from core import observe_quota, arm_detected, connection, sync, list_threads
from desktop_bridge import DesktopBridge, BridgeError

class BridgeTests(unittest.TestCase):
 def test_quota_fail_closed(self):
  with self.assertRaises(ValueError):observe_quota({})
  self.assertFalse(observe_quota({'rateLimits':{'primary':{'usedPercent':100,'resetsAt':1800000000}}})[0])
  self.assertFalse(observe_quota({'rateLimits':{'primary':{'usedPercent':1},'secondary':{'usedPercent':100,'resetsAt':1800000000}}})[0])
  self.assertTrue(observe_quota({'rateLimits':{'primary':{'usedPercent':1},'secondary':{'usedPercent':2}}})[0])
 def test_send_rejects_mismatched_turn_without_mutation(self):
  calls=[]
  b=DesktopBridge()
  b.capabilities=lambda:{'safe_admission':True,'desktop_binding':True}
  def mock(method,params=None):
   calls.append(method)
   if method=='account/rateLimits/read':return {'rateLimits':{'primary':{'usedPercent':0},'secondary':{'usedPercent':0}}}
   if method=='thread/read':return {'thread':{'id':'123e4567-e89b-12d3-a456-426614174000','status':{'type':'idle'},'turns':[{'id':'other','status':'failed','error':{'message':'usageLimitExceeded'}}]}}
   if method=='thread/turns/list':return {'data':[{'id':'other','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}]}
   raise AssertionError('unexpected write: '+method)
  b.request=mock
  with self.assertRaises(BridgeError):b.send_once('123e4567-e89b-12d3-a456-426614174000','expected')
  self.assertEqual(calls,['account/rateLimits/read','thread/read','thread/turns/list'])
 def test_arm_only_enabled_quota_threads(self):
  with tempfile.TemporaryDirectory() as d:
   with closing(connection(pathlib.Path(d)/'private')) as db:
    tid='123e4567-e89b-12d3-a456-426614174000'
    ts=[{'id':tid,'title':'Test','cwd':'','limited':True,'failure_turn':'xyz'}]
    sync(db,ts)
    self.assertEqual(arm_detected(db,ts,{'rateLimits':{'primary':{'usedPercent':100,'resetsAt':1800000000}}},now=1700000000),0)
    from core import enable
    enable(db,tid,True)
    self.assertEqual(arm_detected(db,ts,{'rateLimits':{'primary':{'usedPercent':100,'resetsAt':1800000000}}},now=1700000000),1)
    self.assertEqual(list_threads(db)[0]['state'],'waiting')
if __name__=='__main__':unittest.main()