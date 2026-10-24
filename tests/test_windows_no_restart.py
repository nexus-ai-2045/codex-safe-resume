from contextlib import redirect_stderr
from io import StringIO
import os
import sys
import unittest
from unittest.mock import Mock, patch

import app
import desktop_launcher
import windows_entry


class WindowsNoRestart(unittest.TestCase):
    def test_desktop_launch_command_is_not_exposed_on_either_platform(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform):
                with patch('app.sys.platform', platform), patch.object(
                        sys, 'argv', ['app.py', 'desktop']), patch('app.GUI') as gui, patch(
                        'windows_entry.subprocess.Popen') as spawn, redirect_stderr(StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        app.main()
                self.assertEqual(error.exception.code, 2)
                gui.assert_not_called()
                spawn.assert_not_called()

    def test_windows_module_only_verifies_installation(self):
        self.assertFalse(hasattr(windows_entry, 'launch'))
        self.assertTrue(callable(windows_entry.installation))

    def test_connection_cannot_start_desktop_manager_on_either_platform(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform):
                db, legacy = Mock(), Mock()
                with patch('app.sys.platform', platform), patch.dict(sys.modules, desktop_session=legacy):
                    with self.assertRaises(ValueError):
                        app.start_auto_connection(db, restart_acknowledged=True)
                self.assertEqual(legacy.mock_calls, [])
                self.assertEqual(db.mock_calls, [])

    def test_withdrawn_startup_flag_cannot_activate_bridge_on_either_platform(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform):
                startup = Mock()
                startup.run.return_value = 0
                with patch('desktop_launcher.sys.platform', platform), patch.dict(
                        os.environ, CSR_WINDOWS_ORDER_FREE='1'), patch.dict(
                        sys.modules, windows_startup=startup), patch(
                        'desktop_launcher.connection') as connection, patch(
                        'desktop_launcher.subprocess.Popen') as spawn:
                    with self.assertRaises(ValueError):
                        desktop_launcher.main(['app-server'])
                startup.run.assert_not_called()
                connection.assert_not_called()
                spawn.assert_not_called()

    def test_manager_entry_is_disabled_before_database_access_on_either_platform(self):
        for platform in ('win32', 'darwin'):
            with self.subTest(platform=platform):
                legacy = Mock()
                with patch('desktop_launcher.sys.platform', platform), patch.dict(
                        sys.modules, desktop_session=legacy), patch(
                        'desktop_launcher.connection') as connection:
                    with self.assertRaises(ValueError):
                        desktop_launcher.main(['--manage-desktop', 'fixture-home', 'fixture-nonce'])
                self.assertEqual(legacy.mock_calls, [])
                connection.assert_not_called()
