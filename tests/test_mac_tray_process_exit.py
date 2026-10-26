"""CIの専用Macで実Tk・実トレイと子プロセスの終了を確認する。"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest


CHILD = textwrap.dedent('''
    import json
    import pathlib
    import sys
    import threading
    import tkinter as tk

    import app
    import auto_watch
    import core

    home, route = pathlib.Path(sys.argv[1]), sys.argv[2]
    root = tk.Tk()
    root.withdraw()
    gui = app.GUI.__new__(app.GUI)
    gui.root = root
    gui.db = core.connection(home)
    auto_watch.initialize(gui.db)
    auto_watch.set_enabled(gui.db, True)
    assert auto_watch.status(gui.db)['enabled']
    gui.closing = False
    gui.tray = None
    gui.tray_requested = True
    gui.tray_available = False
    gui.window_hidden = True
    gui.tray_state = tk.StringVar(master=root)
    gui.auto_status = tk.StringVar(master=root)
    gui.navigation_cancel = threading.Event()
    gui.write = lambda message: None
    gui.configure_reopen()
    gui.start_tray()
    assert gui.tray_available and gui.tray.visible
    print(json.dumps({'stage': 'real_tray_ready', 'policy_enabled': auto_watch.status(gui.db)['enabled'],
                      'tk': str(root.tk.call('info', 'patchlevel')), 'python': sys.version.split()[0]}), flush=True)

    def request_exit():
        if route == 'tray':
            # The dependency is hash-pinned. Exercise its real NSMenu action
            # and delegate, rather than invoking the Python callback directly.
            native_menu = gui.tray._menu_handle[0]
            native_menu.performActionForItemAtIndex_(2)
        else:
            root.tk.call('tk::mac::Quit')
        print(json.dumps({'stage': 'exit_requested', 'route': route}), flush=True)

    root.after(250, request_exit)
    gui.run()
    print(json.dumps({'stage': 'mainloop_returned', 'closing': gui.closing}), flush=True)
''')


@unittest.skipUnless(sys.platform == 'darwin' and sys.version_info[:2] == (3, 13)
                     and os.environ.get('CI') == 'true',
                     '配布用Python 3.13のCI専用Macで実トレイを起動する試験')
class MacTrayProcessExit(unittest.TestCase):
    def check_route(self, route):
        with tempfile.TemporaryDirectory(prefix='csr-tray-exit-') as directory:
            home = Path(directory).resolve() / 'state'
            env = os.environ.copy()
            env['CODEX_HOME'] = str(Path(directory) / 'empty-codex')
            env['CODEX_RESUME_HOME'] = str(home)
            result = subprocess.run([sys.executable, '-c', CHILD, str(home), route],
                                    cwd=Path(__file__).resolve().parent.parent,
                                    env=env, capture_output=True, text=True, timeout=20)
            # Do not expose native exception text or paths on assertion failure.
            self.assertEqual(result.returncode, 0, '実Tkとトレイの終了子プロセスが失敗しました')
            stages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            self.assertEqual([item['stage'] for item in stages],
                             ['real_tray_ready', 'exit_requested', 'mainloop_returned'])
            self.assertTrue(stages[0]['policy_enabled'])
            self.assertTrue(stages[-1]['closing'])
            with sqlite3.connect('file:' + str(home / 'resume.sqlite3') + '?mode=ro', uri=True) as db:
                self.assertEqual(db.execute('SELECT enabled FROM auto_watch_policy').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
            print(json.dumps({'schema': 'mac-tray-process-exit/v1', 'route': route,
                              'python': stages[0]['python'], 'tk': stages[0]['tk'],
                              'process_reaped': True, 'auto_resume_off': True, 'attempts': 0}), flush=True)
            # subprocess.run has waited for OS process termination, not just
            # disappearance of the window or a mocked destroy() call.

    def test_real_tray_exit_reaps_process_without_send(self):
        self.check_route('tray')

    def test_real_native_quit_reaps_process_without_send(self):
        self.check_route('native')
