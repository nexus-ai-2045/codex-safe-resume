"""Mac標準終了イベントも既存の停止・終了処理へ渡す。"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
import textwrap
import unittest

import app


CHILD = textwrap.dedent('''
    import json
    from pathlib import Path
    import sys

    import app
    import auto_watch
    from core import connection

    root = app.tk.Tk()
    root.withdraw()
    gui = app.GUI.__new__(app.GUI)
    gui.root = root
    gui.db = connection(Path(sys.argv[1]))
    gui.closing = False
    gui.tray = None
    try:
        auto_watch.initialize(gui.db)
        auto_watch.set_enabled(gui.db, True)
        assert auto_watch.status(gui.db)['enabled']
        gui.configure_reopen()
        assert root.tk.call('info', 'commands', 'tk::mac::Quit')
        root.tk.call('tk::mac::Quit')
        print(json.dumps({'closing': gui.closing}), flush=True)
    finally:
        gui.db.close()
        try:
            root.destroy()
        except app.tk.TclError:
            pass
''')


@unittest.skipUnless(sys.platform == 'darwin' and app.tk is not None,
                     'macOS Tk lifecycle')
class MacQuitLifecycle(unittest.TestCase):
    def test_native_quit_persists_off_before_closing(self):
        with tempfile.TemporaryDirectory(prefix='csr-quit-fixture-') as directory:
            home = Path(directory).resolve() / 'state'
            env = os.environ.copy()
            env['CODEX_HOME'] = str(Path(directory) / 'empty-codex')
            env['CODEX_RESUME_HOME'] = str(home)
            # Native macOS callbacks must not survive into later Tk tests.
            result = subprocess.run([sys.executable, '-c', CHILD, str(home)],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, 'Mac標準終了の子プロセス試験が失敗しました')
            self.assertIs(json.loads(result.stdout)['closing'], True)
            database = (home / 'resume.sqlite3').as_uri() + '?mode=ro'
            with sqlite3.connect(database, uri=True) as saved:
                self.assertEqual(saved.execute(
                    'SELECT enabled FROM auto_watch_policy WHERE singleton=1'
                ).fetchone()[0], 0)
                self.assertEqual(saved.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
