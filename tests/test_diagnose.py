import json
import io
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import closing
import pathlib
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import diagnose
import app
from diagnose import inspection, render_report

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
    def test_explicit_arguments_do_not_resolve_user_home(self):
        with tempfile.TemporaryDirectory() as td, \
                patch('diagnose.pathlib.Path.home', side_effect=RuntimeError('home unavailable')) as home, \
                patch.dict(os.environ, {'CODEX_HOME': 'ignored', 'CODEX_RESUME_HOME': 'ignored'}):
            result = inspection(pathlib.Path(td)/'codex', pathlib.Path(td)/'state')
            home.assert_not_called()
            self.assertEqual(result['checks']['state_directory'], 'not_created')
            self.assertEqual(list(pathlib.Path(td).iterdir()), [])

    def test_explicit_environment_does_not_resolve_user_home(self):
        with tempfile.TemporaryDirectory() as td, \
                patch('diagnose.pathlib.Path.home', side_effect=RuntimeError('home unavailable')) as home, \
                patch.dict(os.environ, {'CODEX_HOME': str(pathlib.Path(td)/'codex'),
                                        'CODEX_RESUME_HOME': str(pathlib.Path(td)/'state')}, clear=True):
            result = inspection()
            home.assert_not_called()
            self.assertEqual(result['checks']['codex_state_db'], 'unavailable')
            self.assertEqual(result['checks']['state_directory'], 'not_created')
            self.assertEqual(list(pathlib.Path(td).iterdir()), [])

    def test_absent_overrides_still_use_user_home_without_creating_files(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True), \
                patch('diagnose.pathlib.Path.home', return_value=pathlib.Path(td)) as home:
            result = inspection()
            self.assertTrue(home.called)
            self.assertEqual(result['checks']['state_directory'], 'not_created')
            self.assertEqual(list(pathlib.Path(td).iterdir()), [])

    def test_empty_overrides_keep_current_directory_semantics(self):
        with patch.dict(os.environ, {'CODEX_HOME': '', 'CODEX_RESUME_HOME': ''}, clear=True), \
                patch('diagnose.pathlib.Path.home', side_effect=RuntimeError('home unavailable')) as home, \
                patch('diagnose.pathlib.Path.is_file', return_value=False):
            result = inspection()
            home.assert_not_called()
            self.assertEqual(result['checks']['codex_state_db'], 'unavailable')
            self.assertIn(result['checks']['state_directory'], ('present', 'permissions_review_required'))

    def test_human_report_is_japanese_without_paths(self):
        with tempfile.TemporaryDirectory() as td:
            result = inspection(pathlib.Path(td)/'private', pathlib.Path(td)/'settings')
            text = render_report(result)
            self.assertNotIn(td, text)
            self.assertNotIn('control_socket:', text)
            self.assertIn('会話データ:', text)

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

    def test_socket_link_is_unverified_without_claiming_native_proxy_cannot_use_it(self):
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)/'codex'
            folder = home/'app-server-control'; folder.mkdir(parents=True)
            try:
                (folder/'app-server-control.sock').symlink_to(folder/'missing')
            except OSError:
                self.skipTest('この環境ではsymlink作成不可')
            result = inspection(home, pathlib.Path(td)/'state')
            self.assertEqual(result['checks']['control_socket'], 'native_proxy_required')
            self.assertFalse(result['desktop_write_validated'])
            self.assertNotIn(td, render_report(result))

class StartupDiagnosticTests(unittest.TestCase):
    def test_text_diagnosis_can_be_redirected_to_cp932_and_keeps_exit_status(self):
        for state, expected in (('available', 0), ('missing', 1)):
            with self.subTest(state=state):
                output = io.BytesIO()
                stream = io.TextIOWrapper(output, encoding='cp932', errors='strict')
                try:
                    with patch('diagnose.inspection', return_value={'checks': {'tkinter': state}}), \
                            patch.object(sys, 'argv', ['app.py', 'diagnose']), \
                            patch.object(sys, 'stdout', stream):
                        self.assertEqual(app.main(), expected)
                    stream.flush()
                    text = output.getvalue().decode('cp932')
                    self.assertIn('実機接続前の診断', text)
                    self.assertIn('画面ライブラリ:', text)
                finally:
                    stream.detach()

    def test_tcl_initialization_failure_is_missing_without_private_details(self):
        fake_tk = SimpleNamespace(Tcl=Mock(side_effect=RuntimeError('private-tcl-path')))
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules, {'tkinter': fake_tk}):
            result = inspection(pathlib.Path(td)/'codex', pathlib.Path(td)/'state')
        fake_tk.Tcl.assert_called_once_with()
        self.assertEqual(result['checks']['tkinter'], 'missing')
        self.assertNotIn('private-tcl-path', json.dumps(result))

    def test_available_requires_tcl_initialization_without_tk_window(self):
        fake_tk = SimpleNamespace(Tcl=Mock(return_value=object()), Tk=Mock())
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules, {'tkinter': fake_tk}):
            result = inspection(pathlib.Path(td)/'codex', pathlib.Path(td)/'state')
        self.assertEqual(result['checks']['tkinter'], 'available')
        fake_tk.Tcl.assert_called_once_with()
        fake_tk.Tk.assert_not_called()

    def test_missing_tk_import_fails_runtime_check(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(sys.modules, {'tkinter': None}):
            result = inspection(pathlib.Path(td)/'codex', pathlib.Path(td)/'state')
        self.assertEqual(result['checks']['tkinter'], 'missing')

    def test_app_diagnosis_returns_runtime_status_before_connection_or_gui(self):
        for state, expected in (('available', 0), ('missing', 1)):
            with self.subTest(state=state), patch('diagnose.inspection', return_value={'checks': {'tkinter': state}}), \
                    patch.object(sys, 'argv', ['app.py', 'diagnose', '--json']), \
                    patch('app.connection') as connection, patch('app.discover') as discover, \
                    patch('app.GUI') as gui, patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(app.main(), expected)
                connection.assert_not_called()
                discover.assert_not_called()
                gui.assert_not_called()

    def test_windowed_entrypoint_honors_exit_status_without_output_streams(self):
        source = str(pathlib.Path(__file__).resolve().parents[1]/'app.py')
        script = """
import runpy, sys
from unittest.mock import patch
import diagnose
result = {'checks': {'tkinter': sys.argv[2]}}
source = sys.argv[1]
with patch.object(diagnose, 'inspection', return_value=result), \
     patch('core.connection', side_effect=AssertionError('DB must not open')), \
     patch('core.discover', side_effect=AssertionError('history must not be read')), \
     patch.object(sys, 'argv', [source, 'diagnose', '--json']), \
     patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
    runpy.run_path(source, run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, CODEX_HOME=str(pathlib.Path(td)/'codex'),
                       CODEX_RESUME_HOME=str(pathlib.Path(td)/'state'))
            for state, expected in (('available', 0), ('missing', 1)):
                with self.subTest(state=state):
                    child = subprocess.run([sys.executable, '-c', script, source, state],
                                           cwd=pathlib.Path(source).parent, env=env,
                                           capture_output=True, timeout=15)
                    self.assertEqual(child.returncode, expected, child.stderr)
            self.assertEqual(list(pathlib.Path(td).iterdir()), [])


if __name__=='__main__': unittest.main()
