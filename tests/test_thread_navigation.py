"""Mac navigation stays in a bounded, owned console child."""
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import desktop_launcher
import thread_navigation as nav

THREAD = '11111111-1111-4111-8111-111111111111'


class ThreadNavigation(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bundle = Path(directory.name).resolve() / 'ChatGPT.app'
        self.exe = self.bundle / 'Contents/MacOS/Codex'
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b'never executed')
        self.info = {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Codex',
                     'CFBundleURLTypes': [{'CFBundleURLSchemes': ['codex']}]}
        self.plist = self.bundle / 'Contents/Info.plist'
        self.write_info()
        self.target_patch = patch.object(nav, '_MAC_BUNDLE', self.bundle)
        self.target_patch.start()
        platform_patch = patch.object(nav.sys, 'platform', 'darwin')
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.addCleanup(self.target_patch.stop)

    def write_info(self):
        self.plist.write_bytes(plistlib.dumps(self.info))

    def test_standard_open_accepts_running_or_unstarted_app(self):
        for _state in ('running', 'unstarted'):
            with patch.object(nav.subprocess, 'Popen') as popen:
                popen.return_value.wait.return_value = 0
                self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + 2), 'requested')
                self.assertEqual(popen.call_args.args[0], [
                    '/usr/bin/open', '-a', str(self.bundle), 'codex://threads/' + THREAD])
                self.assertNotIn('-n', popen.call_args.args[0])
                self.assertNotIn('-W', popen.call_args.args[0])

    def test_bundle_identity_and_scheme_fail_closed(self):
        for key, value in [('CFBundleIdentifier', 'other'), ('CFBundleExecutable', '../bad'),
                           ('CFBundleURLTypes', [])]:
            old = self.info[key]
            self.info[key] = value
            self.write_info()
            with patch.object(nav.subprocess, 'Popen') as popen:
                self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + 2), 'unavailable')
                popen.assert_not_called()
            self.info[key] = old

    def test_executable_and_bundle_symlinks_rejected(self):
        real_binary = self.bundle.parent / 'real-binary'
        self.exe.rename(real_binary)
        self.exe.symlink_to(real_binary)
        self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + 2), 'unavailable')
        self.exe.unlink()
        self.exe.write_bytes(b'never executed')
        real_bundle = self.bundle.parent / 'real.app'
        self.bundle.rename(real_bundle)
        self.bundle.symlink_to(real_bundle, target_is_directory=True)
        self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + 2), 'unavailable')

    def test_mac_public_api_passes_absolute_deadline(self):
        deadline = time.monotonic() + 2
        callback = lambda: True
        with patch.object(nav, '_supervise_mac', return_value='requested') as supervisor:
            self.assertEqual(nav.open_thread(THREAD, before_open=callback, deadline=deadline), 'requested')
            supervisor.assert_called_once_with(THREAD, callback, deadline)

    def test_symlink_or_oversize_plist_rejected(self):
        self.plist.unlink()
        other = self.bundle.parent / 'info'
        other.write_bytes(plistlib.dumps(self.info))
        self.plist.symlink_to(other)
        self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + 2), 'unavailable')
        self.plist.unlink()
        self.plist.write_bytes(b'x' * (1024 * 1024 + 1))
        self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + 2), 'unavailable')

    def test_invalid_ids_never_spawn(self):
        with patch.object(nav.subprocess, 'Popen') as popen:
            for value in (None, '', THREAD + '?x=1', THREAD + '\n', '../elsewhere'):
                with self.assertRaises(nav.ThreadNavigationError):
                    nav.open_thread(value)
            popen.assert_not_called()

    def test_deadline_invalid_or_expired_never_opens(self):
        with patch.object(nav.subprocess, 'Popen') as popen:
            for deadline in (float('nan'), float('inf'), time.monotonic() + 10):
                self.assertEqual(nav.mac_open_chat(THREAD, deadline), 'failed')
            self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() - 1), 'timeout')
            popen.assert_not_called()

    def test_deadline_rechecked_after_bundle_validation(self):
        with patch.object(nav, '_mac_target', side_effect=lambda: time.sleep(.04)), \
                patch.object(nav.subprocess, 'Popen') as popen:
            self.assertEqual(nav.mac_open_chat(THREAD, time.monotonic() + .02), 'timeout')
            popen.assert_not_called()

    def test_helper_dispatch_precedes_db_and_restart_env(self):
        with patch.dict(os.environ, CSR_WINDOWS_ORDER_FREE='1'), \
                patch.object(desktop_launcher, 'connection', side_effect=AssertionError('DB')), \
                patch.object(nav, 'mac_open_chat', return_value='requested') as request, \
                patch('builtins.print') as output:
            deadline = str(time.monotonic() + 2)
            self.assertEqual(desktop_launcher.main(['--open-chat', THREAD, deadline]), 0)
            request.assert_called_once_with(THREAD, deadline)
            output.assert_called_once_with('requested', flush=True)

    def test_actual_console_entry_has_no_db_side_effect_or_restart_env_block(self):
        home = self.bundle.parent / 'unused-state'
        environment = dict(os.environ, CSR_WINDOWS_ORDER_FREE='1', CODEX_RESUME_HOME=str(home))
        result = subprocess.run([sys.executable, '-B', str(Path(desktop_launcher.__file__)),
                                 '--open-chat', THREAD, 'nan'], env=environment,
                                capture_output=True, timeout=3, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [b'failed'])
        self.assertEqual(result.stderr, b'')
        self.assertFalse(home.exists())

    def test_no_tcc_or_deep_verification_imports(self):
        source = Path(nav.__file__).read_text(encoding='utf-8')
        for forbidden in ('AppKit', 'Foundation', 'codesign', 'validate_location', 'AECreateDesc'):
            self.assertNotIn(forbidden, source)


class Supervisor(unittest.TestCase):
    def run_fake(self, code, before_open=None, budget=.15):
        children = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen([sys.executable, '-c', code], **kwargs)
            children.append(child)
            return child
        start = time.monotonic()
        with patch.object(nav, '_NAVIGATION_SECONDS', budget), \
                patch.object(nav.subprocess, 'Popen', side_effect=spawn):
            result = nav._supervise_mac(THREAD, before_open)
        self.assertLess(time.monotonic() - start, budget + .6)
        self.assertTrue(all(child.poll() is not None for child in children))
        return result

    @unittest.skipUnless(os.name == 'posix', 'POSIXプロセスグループの実回収')
    def test_actual_hung_owned_child_is_reaped(self):
        self.assertEqual(self.run_fake('import time; time.sleep(60)'), 'timeout')

    @unittest.skipUnless(os.name == 'posix', 'POSIXプロセスグループの実回収')
    def test_cancel_reaps_owned_child(self):
        calls = []
        def active():
            calls.append(1)
            return len(calls) < 3
        self.assertEqual(self.run_fake('import time; time.sleep(60)', active), 'cancelled')

    @unittest.skipUnless(os.name == 'posix', 'POSIXプロセスグループの実回収')
    def test_fixed_status_and_unexpected_or_oversized_output(self):
        self.assertEqual(self.run_fake('print("requested")', budget=1), 'requested')
        self.assertEqual(self.run_fake('print("requested\\nextra")', budget=1), 'failed')
        self.assertEqual(self.run_fake('print("x"*100000)', budget=.2), 'timeout')

    @unittest.skipUnless(os.name == 'posix', 'POSIXプロセスグループの実回収')
    def test_existing_deadline_is_not_extended(self):
        with patch.object(nav.subprocess, 'Popen') as popen:
            self.assertEqual(nav._supervise_mac(THREAD, None, time.monotonic() - 1), 'timeout')
            popen.assert_not_called()
        remaining = time.monotonic() + .06
        started = time.monotonic()
        with patch.object(nav, '_helper_command', return_value=[sys.executable, '-c',
                'import time; time.sleep(60)']):
            self.assertEqual(nav._supervise_mac(THREAD, None, remaining), 'timeout')
        self.assertLess(time.monotonic() - started, .6)

    def test_missing_helper_and_spawn_failure_have_fixed_status(self):
        with patch.object(nav, '_helper_command', side_effect=FileNotFoundError('private')):
            self.assertEqual(nav._supervise_mac(THREAD, None), 'unavailable')
        with patch.object(nav.subprocess, 'Popen', side_effect=OSError('private')):
            self.assertEqual(nav._supervise_mac(THREAD, None), 'failed')

    def test_frozen_helper_is_sibling_and_source_uses_no_gui_launcher(self):
        deadline = time.monotonic() + 2
        with patch.object(nav.sys, 'frozen', True, create=True), \
                patch.object(nav.sys, 'executable', '/bundle/CodexSafeResume'), \
                patch.object(Path, 'is_file', return_value=True):
            self.assertEqual(nav._helper_command(THREAD, deadline), [
                str(Path('/bundle/CodexSafeResumeBridge')), '--open-chat', THREAD, str(deadline)])
        with patch.object(nav.sys, 'frozen', False, create=True):
            command = nav._helper_command(THREAD, deadline)
            self.assertEqual(command[:2], [sys.executable, '-B'])
            self.assertEqual(Path(command[2]).name, 'desktop_launcher.py')

    def test_cancellation_requires_exact_true(self):
        for value in (False, None, 1, 'yes'):
            with patch.object(nav.subprocess, 'Popen') as popen:
                self.assertEqual(nav._supervise_mac(THREAD, lambda: value), 'cancelled')
                popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
