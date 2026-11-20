"""GUIを生成せず、隔離した実OSロックと公開起動入口を検査する。"""
import contextlib
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
import core


REPOSITORY = Path(__file__).resolve().parents[1]
BOOTSTRAP = '''
import os
import sys
from unittest.mock import patch
import app
import core

mode = sys.argv[1]
class FixtureGUI:
    def __init__(self, **kwargs):
        if mode == 'duplicate':
            raise AssertionError('duplicate reached GUI initialization')
        if mode == 'init-error':
            raise RuntimeError('isolated initialization failure')
    def run(self):
        if mode in ('hold', 'crash'):
            print('ready', flush=True)
            sys.stdin.readline()
        if mode == 'crash':
            os._exit(23)

sys.argv = ['fixture', 'gui', '--no-tray']
with patch.object(app, 'GUI', FixtureGUI), \
     patch.object(app, 'connection', side_effect=AssertionError('startup opened DB')):
    try:
        result = app.main()
    except RuntimeError as error:
        if mode != 'init-error':
            raise
        # Same-process acquisition proves context unwinding after __init__ fails.
        with core.gui_lease() as acquired:
            assert acquired
        print(str(error), flush=True)
    else:
        raise SystemExit(result)
'''


class GuiSingleInstance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'state'

    def environment(self, home=None):
        return dict(os.environ, CODEX_RESUME_HOME=str(home or self.home),
                    PYTHONIOENCODING='utf-8', PYTHONUTF8='1')

    def run_launcher(self, mode, home=None):
        return subprocess.run([sys.executable, '-c', BOOTSTRAP, mode],
                              cwd=REPOSITORY, env=self.environment(home),
                              capture_output=True, text=True, encoding='utf-8', timeout=10)

    def start_holder(self, mode='hold'):
        process = subprocess.Popen([sys.executable, '-c', BOOTSTRAP, mode],
                                   cwd=REPOSITORY, env=self.environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8')
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        self.assertEqual(lines.get(timeout=10), 'ready\n')
        return process

    def test_duplicate_never_constructs_gui_or_changes_database(self):
        with contextlib.closing(core.connection(self.home)) as db:
            from auto_watch import initialize, set_enabled
            initialize(db)
            set_enabled(db, True)
            db.execute("INSERT INTO threads (id,title,enabled,state) VALUES ('fixture','fixture',1,'claimed')")
            db.execute("INSERT INTO attempts VALUES ('fixture','failed','attempt',1,'sending',NULL)")
        owner = self.start_holder()
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        duplicate = self.run_launcher('duplicate')
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertIn('起動済み', duplicate.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)
        self.assertIsNone(owner.poll())

    def test_distinct_state_home_launches_independently(self):
        self.start_holder()
        result = self.run_launcher('once', Path(self.temp.name) / 'other')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_normal_gui_exit_releases_lease_and_preserves_lock_file(self):
        owner = self.start_holder()
        lock = self.home.with_name(self.home.name + '.gui.lock')
        self.assertTrue(lock.exists())
        inode = lock.stat().st_ino
        owner.communicate('\n', timeout=10)
        self.assertEqual(owner.returncode, 0)
        result = self.run_launcher('once')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_initialization_failure_releases_lease(self):
        result = self.run_launcher('init-error')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('isolated initialization failure', result.stdout)
        result = self.run_launcher('once')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixture_process_crash_releases_os_lease(self):
        owner = self.start_holder('crash')
        owner.communicate('\n', timeout=10)
        self.assertEqual(owner.returncode, 23)
        result = self.run_launcher('once')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_exception_releases_lease(self):
        with patch.dict(os.environ, self.environment()), \
                patch.object(sys, 'argv', ['fixture', 'gui']), patch.object(app, 'GUI') as gui:
            gui.return_value.run.side_effect = RuntimeError('isolated run failure')
            with self.assertRaisesRegex(RuntimeError, 'isolated run failure'):
                app.main()
            with core.gui_lease() as acquired:
                self.assertTrue(acquired)

    def test_gui_body_sharing_error_is_not_mistaken_for_busy(self):
        error = OSError('isolated GUI body sharing error')
        error.winerror = 32
        with patch.dict(os.environ, self.environment()), \
                patch.object(sys, 'argv', ['fixture', 'gui']), patch.object(app, 'GUI') as gui:
            gui.return_value.run.side_effect = error
            with self.assertRaises(OSError) as caught:
                app.main()
            self.assertIs(caught.exception, error)

    def test_windows_gui_open_uses_native_error_and_only_sharing_is_busy(self):
        import windows_platform
        for number in (32, 33, 5, 12345, None):
            error = OSError('isolated Windows GUI open error')
            error.winerror = number
            fake_os = SimpleNamespace(name='nt', O_CREAT=os.O_CREAT, O_RDWR=os.O_RDWR,
                                      open=Mock(side_effect=AssertionError('CRT open loses native error')))
            with self.subTest(number=number), patch.object(core, 'os', fake_os), \
                    patch.object(core, 'reject_links'), \
                    patch.object(windows_platform, 'open_gui_lock', side_effect=error, create=True) as opened:
                if number in (32, 33):
                    with core._file_lease(self.home/'fixture.lock', gui=True) as acquired:
                        self.assertFalse(acquired)
                else:
                    with self.assertRaises(OSError) as caught:
                        with core._file_lease(self.home/'fixture.lock', gui=True):
                            self.fail('invalid native error must raise')
                    self.assertIs(caught.exception, error)
                opened.assert_called_once_with(self.home/'fixture.lock')
                fake_os.open.assert_not_called()

    def test_removed_state_is_not_recreated_by_busy_launcher(self):
        with core.gui_lease(self.home) as acquired:
            self.assertTrue(acquired)
            self.home.rmdir()
            result = self.run_launcher('duplicate')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('設定削除中', result.stderr)
            self.assertFalse(self.home.exists())

    def test_monitor_lease_is_independent_of_gui_lease(self):
        self.start_holder()
        with contextlib.closing(core.connection(self.home)) as db:
            with core.monitor_lease(db) as acquired:
                self.assertTrue(acquired)
                with core.monitor_lease(db) as second:
                    self.assertFalse(second)

    def test_duplicate_without_stderr_exits_normally(self):
        self.start_holder()
        with patch.dict(os.environ, self.environment()), patch.object(sys, 'stderr', None), \
                patch.object(sys, 'argv', ['fixture', 'gui']), patch.object(app, 'GUI') as gui:
            self.assertEqual(app.main(), 0)
        gui.assert_not_called()

    def test_default_launch_holds_lease_until_run_returns(self):
        # Exercise default/no-argument route with real OS contention in another process.
        def run():
            result = self.run_launcher('duplicate')
            self.assertEqual(result.returncode, 0, result.stderr)
        with patch.dict(os.environ, self.environment()), \
                patch.object(sys, 'argv', ['fixture']), patch.object(app, 'GUI') as gui:
            gui.return_value.run.side_effect = run
            app.main()
        gui.assert_called_once_with(enable_tray=True)

    def test_unsafe_lock_refused_before_gui(self):
        folder = core.safe_state_dir(self.home)
        lock = folder.with_name(folder.name + '.gui.lock')
        target = folder / 'target'
        target.write_bytes(b'preserved')
        for kind in ('symlink', 'hardlink', 'directory', 'mode'):
            if kind == 'mode' and os.name != 'posix':
                continue
            with self.subTest(kind=kind):
                try:
                    if kind == 'symlink':
                        try:
                            lock.symlink_to(target)
                        except OSError:
                            if os.name == 'nt':
                                continue  # Windows link creation may require developer mode.
                            raise
                    elif kind == 'hardlink':
                        os.link(target, lock)
                    elif kind == 'directory':
                        lock.mkdir()
                    else:
                        lock.touch(mode=0o600)
                        lock.chmod(0o644)
                    with patch.dict(os.environ, self.environment()), \
                            patch.object(sys, 'argv', ['fixture', 'gui']), \
                            patch.object(app, 'GUI') as gui:
                        with self.assertRaises((RuntimeError, OSError)):
                            app.main()
                        gui.assert_not_called()
                    self.assertEqual(target.read_bytes(), b'preserved')
                    self.assertFalse((folder / 'resume.sqlite3').exists())
                finally:
                    if lock.is_dir() and not lock.is_symlink():
                        lock.rmdir()
                    elif lock.exists() or lock.is_symlink():
                        lock.unlink()


if __name__ == '__main__':
    unittest.main()
