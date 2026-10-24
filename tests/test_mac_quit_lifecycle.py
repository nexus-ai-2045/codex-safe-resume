"""Mac標準終了イベントも既存の停止・終了処理へ渡す。"""
import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest

import app
import auto_watch
from core import connection


@unittest.skipUnless(sys.platform == 'darwin' and app.tk is not None,
                     'macOS Tk lifecycle')
class MacQuitLifecycle(unittest.TestCase):
    def test_native_quit_persists_off_before_closing(self):
        with tempfile.TemporaryDirectory(prefix='csr-quit-fixture-') as directory:
            root = app.tk.Tk()
            root.withdraw()
            gui = app.GUI.__new__(app.GUI)
            gui.root = root
            gui.db = connection(Path(directory))
            gui.closing = False
            gui.tray = None
            try:
                auto_watch.initialize(gui.db)
                auto_watch.set_enabled(gui.db, True)
                gui.configure_reopen()
                self.assertTrue(root.tk.call('info', 'commands', 'tk::mac::Quit'))
                root.tk.call('tk::mac::Quit')
                self.assertTrue(gui.closing)
                with sqlite3.connect(Path(directory) / 'resume.sqlite3') as saved:
                    self.assertEqual(saved.execute(
                        'SELECT enabled FROM auto_watch_policy WHERE singleton=1'
                    ).fetchone()[0], 0)
                    self.assertEqual(saved.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
            finally:
                gui.db.close()
                try:
                    root.destroy()
                except app.tk.TclError:
                    pass
