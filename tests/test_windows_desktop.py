import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import windows_desktop


class WindowsDiscoveryTests(unittest.TestCase):
    def test_startup_arguments_require_the_attested_cli_executable(self):
        identities = {
            20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
            30: dict(pid=30, ppid=20, start=[2, 0], executable='/fixture/codex.exe'),
        }
        for executable in ('/fixture/codex.exe', '/fixture/other.exe'):
            with self.subTest(executable=executable), \
                    patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                    patch('windows_desktop.subprocess.run', return_value=self.reply(commandLine='fixture')), \
                    patch('windows_desktop.validate_location', side_effect=Path), \
                    patch('windows_platform.process_identity', side_effect=identities.__getitem__), \
                    patch('windows_platform.command_line_arguments', return_value=[executable, 'app-server']):
                if executable == '/fixture/codex.exe':
                    result = windows_desktop.resolve_desktop(include_identity=True, include_arguments=True)
                    self.assertEqual(result[-1], ['app-server'])
                else:
                    with self.assertRaises(ValueError):
                        windows_desktop.resolve_desktop(include_identity=True, include_arguments=True)

    def reply(self, **changes):
        value = dict(desktop='/fixture/Desktop.exe', cli='/fixture/codex.exe',
                     desktopPid=20, cliPid=30)
        value.update(changes)
        return subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b'')

    def test_discovery_can_return_exact_verified_main_authority(self):
        identities = {
            20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
            30: dict(pid=30, ppid=20, start=[2, 0], executable='/fixture/codex.exe'),
        }
        with patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                patch('windows_desktop.subprocess.run', return_value=self.reply()), \
                patch('windows_desktop.validate_location', side_effect=Path), \
                patch('windows_platform.process_identity', side_effect=identities.__getitem__):
            result = windows_desktop.resolve_desktop(include_identity=True)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[3], identities[20])
        self.assertIsNot(result[3], identities[20])

    def test_discovery_validates_both_executables_and_processes(self):
        identities = {
            20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
            30: dict(pid=30, ppid=20, start=[2, 0], executable='/fixture/codex.exe'),
        }
        with patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                patch('windows_desktop.subprocess.run', return_value=self.reply()) as run, \
                patch('windows_desktop.validate_location', side_effect=Path) as validate, \
                patch('windows_platform.process_identity', side_effect=identities.__getitem__):
            cli, desktop, identity = windows_desktop.resolve_desktop()
        self.assertEqual(cli, Path('/fixture/codex.exe'))
        self.assertEqual(desktop, Path('/fixture/Desktop.exe'))
        self.assertEqual(identity, str(desktop))
        self.assertEqual(validate.call_count, 2)
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertIn('-NoProfile', run.call_args.args[0])

    def test_ambiguous_missing_malformed_and_nonzero_results_fail_closed(self):
        for reply in (self.reply(cliPid=0), self.reply(desktopPid=True),
                      subprocess.CompletedProcess([], 0, b'[]', b''),
                      subprocess.CompletedProcess([], 1, b'private detail', b'')):
            with self.subTest(reply=reply.returncode), \
                    patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                    patch('windows_desktop.subprocess.run', return_value=reply):
                with self.assertRaisesRegex(ValueError, 'Desktop') as error:
                    windows_desktop.resolve_desktop()
                self.assertNotIn('private', str(error.exception))

    def test_unrelated_or_replaced_process_is_never_adopted(self):
        for parent, executable in ((99, '/fixture/codex.exe'), (20, '/changed/codex.exe')):
            identities = {
                20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
                30: dict(pid=30, ppid=parent, start=[2, 0], executable=executable),
            }
            with patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                    patch('windows_desktop.subprocess.run', return_value=self.reply()), \
                    patch('windows_desktop.validate_location', side_effect=Path), \
                    patch('windows_platform.process_identity', side_effect=identities.__getitem__):
                with self.assertRaises(ValueError):
                    windows_desktop.resolve_desktop()

    def test_filetime_low_word_rollover_preserves_creation_order(self):
        identities = {
            20: dict(pid=20, ppid=1, start=[0xffffffff, 1], executable='/fixture/Desktop.exe'),
            30: dict(pid=30, ppid=20, start=[0, 2], executable='/fixture/codex.exe'),
        }
        with patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                patch('windows_desktop.subprocess.run', return_value=self.reply()), \
                patch('windows_desktop.validate_location', side_effect=Path), \
                patch('windows_platform.process_identity', side_effect=identities.__getitem__):
            self.assertEqual(windows_desktop.resolve_desktop()[0], Path('/fixture/codex.exe'))

    def test_managed_bridge_chain_is_reattested_before_reconnect(self):
        for actual_bridge in ('/fixture/bridge.exe', '/other/bridge.exe'):
            identities = {
                20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
                25: dict(pid=25, ppid=20, start=[2, 0], executable=actual_bridge),
                30: dict(pid=30, ppid=25, start=[3, 0], executable='/fixture/codex.exe'),
            }
            with self.subTest(accepted=actual_bridge == '/fixture/bridge.exe'), \
                    patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                    patch('windows_desktop.subprocess.run', return_value=self.reply(bridgePids=[25])), \
                    patch('windows_desktop.validate_location', side_effect=Path), \
                    patch('windows_platform.process_identity', side_effect=identities.__getitem__):
                if actual_bridge == '/fixture/bridge.exe':
                    self.assertEqual(windows_desktop.resolve_desktop(bridge=Path(actual_bridge))[0],
                                     Path('/fixture/codex.exe'))
                else:
                    with self.assertRaises(ValueError):
                        windows_desktop.resolve_desktop(bridge=Path('/fixture/bridge.exe'))

    def test_onefile_bootloader_chain_is_bounded_and_every_process_is_checked(self):
        identities = {
            20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
            25: dict(pid=25, ppid=20, start=[2, 0], executable='/fixture/bridge.exe'),
            26: dict(pid=26, ppid=25, start=[3, 0], executable='/fixture/bridge.exe'),
            30: dict(pid=30, ppid=26, start=[4, 0], executable='/fixture/codex.exe'),
        }
        for chain in ([26, 25], [26, 26], [26], [26, 25, 20]):
            with self.subTest(chain=chain), \
                    patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                    patch('windows_desktop.subprocess.run', return_value=self.reply(bridgePids=chain)), \
                    patch('windows_desktop.validate_location', side_effect=Path), \
                    patch('windows_platform.process_identity', side_effect=identities.__getitem__):
                if chain == [26, 25]:
                    self.assertEqual(windows_desktop.resolve_desktop(bridge=Path('/fixture/bridge.exe'))[0],
                                     Path('/fixture/codex.exe'))
                else:
                    with self.assertRaises(ValueError):
                        windows_desktop.resolve_desktop(bridge=Path('/fixture/bridge.exe'))

    def test_store_package_proof_is_required_before_staging_cached_cli(self):
        identities = {
            20: dict(pid=20, ppid=1, start=[1, 0], executable='/fixture/Desktop.exe'),
            30: dict(pid=30, ppid=20, start=[2, 0], executable='/fixture/codex.exe'),
        }
        def location(path):
            if Path(path) in (Path('/fixture/Desktop.exe'), Path('/fixture/codex.exe')):
                raise ValueError('unverified ACL')
            return Path(path)
        for proof in ({'root': '/verified-package'}, ValueError('unverified package')):
            with self.subTest(proof_valid=isinstance(proof, dict)), \
                    patch('cli_security._system_powershell', return_value=Path('/system/powershell.exe')), \
                    patch('windows_desktop.subprocess.run', return_value=self.reply()), \
                    patch('windows_desktop.validate_location', side_effect=location), \
                    patch('windows_platform.process_identity', side_effect=identities.__getitem__), \
                    patch('windows_package.verify_package', side_effect=proof if isinstance(proof, Exception) else None,
                          return_value=proof) as verify, \
                    patch('windows_runtime.stage_cli', return_value=Path('/protected/codex.exe')) as stage:
                if isinstance(proof, dict):
                    self.assertEqual(windows_desktop.resolve_desktop()[0], Path('/protected/codex.exe'))
                    verify.assert_called_once_with(20, Path('/fixture/Desktop.exe'))
                    stage.assert_called_once()
                else:
                    with self.assertRaises(ValueError):
                        windows_desktop.resolve_desktop()
                    stage.assert_not_called()
