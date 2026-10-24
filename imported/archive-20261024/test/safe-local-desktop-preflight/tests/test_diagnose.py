import json
from contextlib import closing
import pathlib
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diagnose import inspection

class DiagnosticTests(unittest.TestCase):
    def test_missing_files_and_no_leaks(self):
        with tempfile.TemporaryDirectory() as td:
            codex = pathlib.Path(td) / 'PRIVATE-ID-DO-NOT-PRINT'
            out = inspection(codex, pathlib.Path(td) / 'state', os_name='Windows')
            encoded = json.dumps(out)
            self.assertNotIn('PRIVATE-ID-DO-NOT-PRINT', encoded)
            self.assertNotIn(td, encoded)
            self.assertEqual(out['checks']['control_socket'], 'unavailable')
            self.assertFalse(out['real_quota_e2e_validated'])
    def test_supported_schema(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'codex'; p.mkdir()
            with closing(sqlite3.connect(p / 'state_5.sqlite')) as db:
                db.execute('CREATE TABLE threads(id TEXT, title TEXT, cwd TEXT)')
            result = inspection(p, pathlib.Path(td) / 'not-created')
            self.assertEqual(result['checks']['thread_schema'], 'supported')
    def test_unknown_schema_stops(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / 'codex'; p.mkdir()
            with closing(sqlite3.connect(p / 'state_5.sqlite')) as db:
                db.execute('CREATE TABLE threads(id TEXT)')
            self.assertEqual(inspection(p, pathlib.Path(td))['checks']['thread_schema'], 'unsupported')

if __name__=='__main__': unittest.main()