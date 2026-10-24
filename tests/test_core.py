import os,pathlib,sqlite3,stat,sys,tempfile,time,unittest
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from core import *
T='123e4567-e89b-12d3-a456-426614174000'
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.db=connection(pathlib.Path(self.tmp.name)/'private')
  self.addCleanup(self.db.close)
  self.sample=dict(id=T,title='Project',cwd='/tmp/project',limited=True,failure_turn='01ABCDE')
  sync(self.db,[self.sample]);enable(self.db,T,True)
 def test_discovery_preserves_optin(self):
  sync(self.db,[self.sample]);self.assertEqual(list_threads(self.db)[0]['enabled'],1)
 def test_no_sql_injection(self):
  a=dict(self.sample,title="x'); DROP TABLE threads;--");sync(self.db,[a]);self.assertEqual(len(list_threads(self.db)),1)
 def test_unknown_id(self):
  with self.assertRaises(ValueError):enable(self.db,'../../oops',True)
 def test_completion(self):
  mark(self.db,T,'completed');self.assertEqual(list_threads(self.db)[0]['enabled'],0)
  with self.assertRaises(ValueError):enable(self.db,T,True)
 def test_quota(self):
  quota(self.db,T,'01ABCDE',1800000000);self.assertEqual(list_threads(self.db)[0]['state'],'waiting')
 def test_fail_change_blocks(self):
  quota(self.db,T,'01ABCDE',1800000000)
  sync(self.db,[dict(self.sample,failure_turn='01ABCDF')]);self.assertEqual(list_threads(self.db)[0]['state'],'blocked')
 def test_finish_blocks(self):
  quota(self.db,T,'01ABCDE',1800000000)
  sync(self.db,[dict(self.sample,limited=False,failure_turn=None)]);self.assertEqual(list_threads(self.db)[0]['state'],'blocked')
 def test_dry_run(self):
  quota(self.db,T,'01ABCDE',1700000000);self.assertEqual(dry_run(self.db,[self.sample]),[T]);self.assertEqual(list_threads(self.db)[0]['state'],'waiting')
 def test_atomic_once(self):
  quota(self.db,T,'01ABCDE',1700000000)
  self.assertEqual(ready(self.db,[self.sample]),[T]);self.assertEqual(ready(self.db,[self.sample]),[])
 def test_no_unknown_retry(self):
  quota(self.db,T,'01ABCDE',1700000000);ready(self.db,[self.sample]);self.assertEqual(list_threads(self.db)[0]['state'],'claimed')
 def test_read_only_discovery(self):
  home=pathlib.Path(self.tmp.name)/'codex';home.mkdir()
  state=sqlite3.connect(home/'state_5.sqlite');state.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT)');state.execute('INSERT INTO threads VALUES (?,?,?)',(T,'title','/tmp'));state.commit();state.close()
  hist=sqlite3.connect(home/'thread_history_1.sqlite');hist.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT)');hist.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',(T,'01A','failed','{"type":"usageLimitExceeded"}'));hist.commit();hist.close()
  rows,err=discover(home);self.assertIsNone(err);self.assertTrue(rows[0]['limited'])
  hist=sqlite3.connect(home/'thread_history_1.sqlite');hist.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',(T,'01B','completed',None));hist.commit();hist.close()
  rows,err=discover(home);self.assertFalse(rows[0]['limited'])
 def test_unsafe_root_symlink(self):
  import subprocess
  import cli_security
  with tempfile.TemporaryDirectory() as p:
   link=pathlib.Path(p)/'link'
   target=pathlib.Path(self.tmp.name)
   before=set(target.rglob('*'))
   if os.name=='nt':
    subprocess.run(
     [str(cli_security._system_powershell()),'-NoProfile','-NonInteractive',
      '-Command',"$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path $env:CSR_TEST_JUNCTION_PATH -Target $env:CSR_TEST_JUNCTION_TARGET | Out-Null"],
     check=True,shell=False,stdin=subprocess.DEVNULL,
     stdout=subprocess.PIPE,stderr=subprocess.PIPE,
     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
     env=dict(os.environ,CSR_TEST_JUNCTION_PATH=str(link),CSR_TEST_JUNCTION_TARGET=str(target)))
   else:
    link.symlink_to(target,target_is_directory=True)
   try:
    if os.name=='nt':
     info=link.lstat()
     self.assertTrue(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
     self.assertEqual(info.st_reparse_tag,stat.IO_REPARSE_TAG_MOUNT_POINT)
    with self.assertRaises(RuntimeError):safe_state_dir(link)
    self.assertEqual(set(target.rglob('*')),before)
   finally:
    if os.name=='nt':link.rmdir()
if __name__=='__main__':unittest.main()
