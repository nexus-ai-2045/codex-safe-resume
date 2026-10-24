from contextlib import ExitStack
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import windows_desktop


class WindowsStartupCallerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Path(Path.cwd().anchor)/'startup-fixture'
        self.root = self.fixture/'package'
        self.desktop = self.root/'app'/'ChatGPT.exe'
        self.cli = self.root/'app'/'resources'/'codex.exe'
        self.bridge = self.fixture/'bridge.exe'
        self.identities = {
            10: self.identity(10, 20, 3, self.bridge),
            20: self.identity(20, 1, 1, self.desktop),
        }

    def identity(self, pid, ppid, start, executable):
        return dict(pid=pid, ppid=ppid, start=[start, 0], executable=str(executable))

    def run_resolver(self, *, process=None, package=None, location=None, signature=None, stage=True,
                     registered=None):
        with ExitStack() as stack:
            stack.enter_context(patch('windows_entry.installation',
                                      side_effect=ValueError('unregistered') if registered is None else None,
                                      return_value=registered))
            stack.enter_context(patch('windows_desktop.os.getpid', return_value=10))
            stack.enter_context(patch('windows_platform.process_identity',
                                      side_effect=process or self.identities.__getitem__))
            stack.enter_context(patch('windows_package.verify_package',
                                      side_effect=package if isinstance(package, Exception) else None,
                                      return_value=package or dict(root=str(self.root))))
            stack.enter_context(patch('windows_desktop.validate_location', side_effect=location or Path))
            stack.enter_context(patch('cli_security._system_powershell',
                                      return_value=self.fixture/'system'/'powershell.exe'))
            stack.enter_context(patch('windows_desktop.subprocess.run',
                                      return_value=signature or subprocess.CompletedProcess([], 0, b'true')))
            return windows_desktop.resolve_startup_caller(self.bridge, self.fixture/'home', stage=stage)

    def test_direct_signed_registered_desktop_is_accepted(self):
        result = self.run_resolver()
        self.assertEqual(result[:3], (self.cli, self.desktop, str(self.desktop)))
        self.assertEqual(result[3], self.identities[20])
        self.assertIsNot(result[3], self.identities[20])

    def test_unpacked_process_requires_exact_signed_registered_installation(self):
        self.assertEqual(self.run_resolver(package=ValueError('no package identity'),
                                          registered=self.desktop, stage=False)[0], self.cli)
        with self.assertRaises(ValueError):
            self.run_resolver(package=ValueError('no package identity'),
                              registered=self.fixture/'other'/'ChatGPT.exe', stage=False)

    def test_pyinstaller_onefile_runtime_and_bootloader_are_accepted(self):
        self.identities[10]['ppid'] = 15
        self.identities[15] = self.identity(15, 20, 2, self.bridge)
        self.assertEqual(self.run_resolver()[3], self.identities[20])

    def test_third_bridge_ancestor_and_cycles_are_rejected(self):
        self.identities[10]['ppid'] = 15
        self.identities[15] = self.identity(15, 16, 2, self.bridge)
        for parent in (10, 20):
            with self.subTest(parent=parent):
                self.identities[16] = self.identity(16, parent, 1, self.bridge)
                with self.assertRaises(ValueError):
                    self.run_resolver()

    def test_arbitrary_parent_and_wrong_package_path_are_rejected(self):
        for path in (self.fixture/'fake'/'ChatGPT.exe', self.cli,
                     self.root/'app'/'Codex.exe', self.fixture/'shell.exe'):
            with self.subTest(path=path):
                self.identities[20]['executable'] = str(path)
                with self.assertRaises(ValueError):
                    self.run_resolver()

    def test_current_process_must_be_the_pinned_bridge(self):
        self.identities[10]['executable'] = str(self.fixture/'other'/'bridge.exe')
        with self.assertRaises(ValueError):
            self.run_resolver()

    def test_later_parent_creation_is_rejected_and_filetime_rollover_is_ordered(self):
        self.identities[20]['start'] = [4, 0]
        with self.assertRaises(ValueError):
            self.run_resolver()
        self.identities[20]['start'] = [0xffffffff, 1]
        self.identities[10]['start'] = [0, 2]
        self.assertEqual(self.run_resolver()[3], self.identities[20])

    def test_every_ancestry_identity_is_rechecked_for_pid_reuse(self):
        self.identities[10]['ppid'] = 15
        self.identities[15] = self.identity(15, 20, 2, self.bridge)
        for replaced in (10, 15, 20):
            seen = {}
            def process(pid):
                seen[pid] = seen.get(pid, 0) + 1
                value = dict(self.identities[pid])
                if pid == replaced and seen[pid] > 1:
                    value['start'] = [99, 0]
                return value
            with self.subTest(replaced=replaced), self.assertRaises(ValueError):
                self.run_resolver(process=process)

    def test_native_same_user_validation_failure_is_not_bypassed(self):
        def process(pid):
            if pid == 20:
                raise ValueError('different user')
            return self.identities[pid]
        with self.assertRaisesRegex(ValueError, 'Desktop'):
            self.run_resolver(process=process)

    def test_invalid_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_resolver(signature=subprocess.CompletedProcess([], 0, b'false'))

    def test_only_rejected_package_cli_location_uses_protected_staging(self):
        staged = self.fixture/'protected'/'codex.exe'
        def location(path):
            if path == self.cli:
                raise ValueError('Store ACL')
            return Path(path)
        with patch('windows_runtime.stage_cli', return_value=staged) as stage:
            self.assertEqual(self.run_resolver(location=location)[0], staged)
        stage.assert_called_once_with(str(self.root), self.cli, self.fixture/'home')

    def test_valid_package_cli_is_not_copied(self):
        with patch('windows_runtime.stage_cli') as stage:
            self.run_resolver()
        stage.assert_not_called()

    def test_bridge_location_validation_failure_does_not_allow_staging(self):
        with patch('windows_runtime.stage_cli') as stage:
            with self.assertRaises(ValueError):
                self.run_resolver(location=lambda path: (_ for _ in ()).throw(ValueError('ACL')))
        stage.assert_not_called()

    def test_package_proof_failure_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_resolver(package=ValueError('package'))

    def test_signature_verification_is_noninteractive_and_does_not_use_a_shell(self):
        with patch('windows_desktop.subprocess.run',
                   return_value=subprocess.CompletedProcess([], 0, b'true')) as run:
            with ExitStack() as stack:
                stack.enter_context(patch('windows_desktop.os.getpid', return_value=10))
                stack.enter_context(patch('windows_platform.process_identity',
                                          side_effect=self.identities.__getitem__))
                stack.enter_context(patch('windows_package.verify_package',
                                          return_value=dict(root=str(self.root))))
                stack.enter_context(patch('windows_desktop.validate_location', side_effect=Path))
                stack.enter_context(patch('cli_security._system_powershell',
                                          return_value=self.fixture/'powershell.exe'))
                windows_desktop.resolve_startup_caller(self.bridge)
        self.assertFalse(run.call_args.kwargs['shell'])
        self.assertIn('-NoProfile', run.call_args.args[0])
        self.assertIn('-NonInteractive', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env']['CSR_CLI_TO_VERIFY'], str(self.cli))
        self.assertEqual(run.call_args.kwargs['timeout'], 15)

    def test_malformed_native_identity_and_signature_errors_fail_closed(self):
        for field, value in (('pid', True), ('ppid', 0), ('start', [1]),
                             ('start', [True, 0]), ('start', [0, 0]),
                             ('executable', 'relative.exe')):
            original = self.identities[20][field]
            self.identities[20][field] = value
            try:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.run_resolver()
            finally:
                self.identities[20][field] = original
        for reply in (subprocess.CompletedProcess([], 1, b'true'),
                      subprocess.CompletedProcess([], 0, b'true\n')):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                self.run_resolver(signature=reply)

    def test_discovery_of_other_desktops_is_never_invoked(self):
        with patch('windows_desktop.resolve_desktop', side_effect=AssertionError('global discovery')):
            self.assertEqual(self.run_resolver()[3], self.identities[20])

    def test_original_package_cli_can_be_verified_without_cache_or_cli_acl_access(self):
        def location(path):
            if path != self.bridge:
                raise AssertionError('original CLI must use package trust')
            return Path(path)
        with patch('windows_runtime.stage_cli', side_effect=OSError('cache unavailable')) as stage:
            self.assertEqual(self.run_resolver(location=location, stage=False)[0], self.cli)
        stage.assert_not_called()

    def test_original_package_cli_still_requires_signature_package_and_ancestry(self):
        with self.assertRaises(ValueError):
            self.run_resolver(stage=False, signature=subprocess.CompletedProcess([], 0, b'false'))
        with self.assertRaises(ValueError):
            self.run_resolver(stage=False, package=ValueError('package'))
        with self.assertRaises(ValueError):
            self.run_resolver(stage=False, package=dict(root=str(self.fixture/'other')))
        self.identities[20]['start'] = [99, 0]
        with self.assertRaises(ValueError):
            self.run_resolver(stage=False)

    def test_original_package_cli_rejects_links_and_reused_parent(self):
        with patch('core.reject_links', side_effect=RuntimeError('reparse point')):
            with self.assertRaises(ValueError):
                self.run_resolver(stage=False)
        seen = {}
        def process(pid):
            seen[pid] = seen.get(pid, 0) + 1
            value = dict(self.identities[pid])
            if pid == 20 and seen[pid] > 1:
                value['start'] = [99, 0]
            return value
        with self.assertRaises(ValueError):
            self.run_resolver(stage=False, process=process)
