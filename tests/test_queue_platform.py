import os
import pathlib
import plistlib
import types
import tempfile
import sys
import ctypes
import errno
import hashlib
import unittest
import uuid
from unittest import mock

import queue_platform as platform


class QueuePlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        for name in ('state_5.sqlite', 'queue_1.sqlite'):
            (self.home / name).write_bytes(b'fixture')
        self.thread = str(uuid.uuid4())
        (self.home / 'thread-writer-locks').mkdir()
        (self.home / 'thread-writer-locks' / (self.thread + '.lock')).write_bytes(b'')
        self.desktop = {'pid': 10, 'ppid': 1, 'start': [100, 1], 'executable': str(self.home / 'ChatGPT.exe')}
        self.server = {'pid': 20, 'ppid': 10, 'start': [200, 1], 'executable': str(self.home / 'codex.exe')}
        self.chain = [self.server, self.desktop]
        self.identities = {10: self.desktop, 20: self.server}
        self.holders = [{'pid': 20, 'start': [200, 1]}]
        patches = [mock.patch.object(platform, '_platform', return_value='win32'),
                   mock.patch.object(platform, '_identity', side_effect=lambda pid: dict(self.identities[pid])),
                   mock.patch.object(platform, '_server_role', return_value='app-server'),
                   mock.patch.object(platform, '_attested_files_match', return_value=True),
                   mock.patch.object(platform, '_holders', side_effect=lambda paths: list(self.holders)),
                   mock.patch.object(platform, '_trusted_chain', side_effect=lambda held, team: {'desktop': self.desktop, 'server': self.server, 'chain': self.chain, 'executable': self.server['executable']})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_selects_requested_home_without_global_singleton(self):
        result = platform.desktop_for_home(self.home)
        self.assertEqual(result['desktop'], self.desktop)
        self.assertEqual(result['server'], self.server)
        self.assertEqual(result['home'], str(self.home))
        self.assertTrue(platform.revalidate(result))

    def test_revalidation_does_not_repeat_full_signature_or_runtime_staging(self):
        with mock.patch.object(platform, '_trusted_chain', wraps=platform._trusted_chain) as trusted:
            evidence = platform.desktop_for_home(self.home)
            self.assertTrue(platform.revalidate(evidence))
            self.assertTrue(platform.loaded(self.thread, evidence))
            self.assertEqual(trusted.call_count, 1)

    def test_revalidation_rejects_changed_or_unavailable_attestation(self):
        evidence = platform.desktop_for_home(self.home)
        with mock.patch.object(platform, '_attested_files_match', return_value=False):
            self.assertFalse(platform.revalidate(evidence))
        with mock.patch.object(platform, '_attested_files_match', side_effect=OSError('unavailable')):
            self.assertFalse(platform.revalidate(evidence))

    def test_known_server_role_is_not_requeried_but_other_backend_is_checked(self):
        evidence = platform.desktop_for_home(self.home)
        with mock.patch.object(platform, '_server_role', side_effect=ValueError('unexpected repeat')) as role:
            self.assertTrue(platform.revalidate(evidence))
            role.assert_not_called()
            self.identities[21] = dict(self.server, pid=21)
            self.holders.append({'pid': 21, 'start': [200, 1]})
            self.assertFalse(platform.revalidate(evidence))
            role.assert_called_once()

    def test_unrelated_file_holder_is_not_another_backend(self):
        other = {'pid': 30, 'ppid': 1, 'start': [300, 1], 'executable': str(self.home / 'reader.exe')}
        self.identities[30] = other
        self.holders.append({'pid': 30, 'start': [300, 1]})
        self.assertEqual(platform.desktop_for_home(self.home)['server'], self.server)

    def test_two_backend_holders_fail_closed(self):
        self.identities[21] = dict(self.server, pid=21)
        self.holders.append({'pid': 21, 'start': [200, 1]})
        with self.assertRaises(ValueError):
            platform.desktop_for_home(self.home)

    def test_native_non_desktop_server_is_excluded(self):
        self.identities[30] = dict(self.server, pid=30, ppid=40)
        self.identities[40] = dict(self.desktop, pid=40, executable=str(self.home / 'python.exe'))
        self.holders.append({'pid': 30, 'start': [200, 1]})
        self.assertEqual(platform.desktop_for_home(self.home)['server'], self.server)

    def test_exec_server_is_excluded_and_unknown_role_is_denied(self):
        self.identities[21] = dict(self.server, pid=21)
        self.holders.append({'pid': 21, 'start': [200, 1]})
        with mock.patch.object(platform, '_server_role', side_effect=lambda item: 'exec-server' if item['pid'] == 21 else 'app-server'):
            self.assertEqual(platform.desktop_for_home(self.home)['server'], self.server)
        with mock.patch.object(platform, '_server_role', side_effect=ValueError('unknown role')):
            with self.assertRaises(ValueError):
                platform.desktop_for_home(self.home)

    def test_proven_exited_resource_holder_does_not_hide_live_desktop(self):
        from windows_platform import _ProcessExited
        self.holders.append({'pid': 30, 'start': [300, 1]})

        def identity(pid):
            if pid == 30:
                raise _ProcessExited('exited')
            return dict(self.identities[pid])

        with mock.patch.object(platform, '_identity', side_effect=identity):
            self.assertEqual(platform.desktop_for_home(self.home)['server'], self.server)

    def test_unreadable_resource_holder_is_not_silently_excluded(self):
        self.holders.append({'pid': 30, 'start': [300, 1]})

        def identity(pid):
            if pid == 30:
                raise ValueError('unavailable')
            return dict(self.identities[pid])

        with mock.patch.object(platform, '_identity', side_effect=identity):
            with self.assertRaises(ValueError):
                platform.desktop_for_home(self.home)

    def test_proven_exited_parent_is_not_a_live_desktop_chain(self):
        from windows_platform import _ProcessExited
        self.identities[30] = dict(self.server, pid=30, ppid=40)
        self.holders.append({'pid': 30, 'start': self.server['start']})

        def identity(pid):
            if pid == 40:
                raise _ProcessExited('exited')
            return dict(self.identities[pid])

        with mock.patch.object(platform, '_identity', side_effect=identity):
            self.assertEqual(platform.desktop_for_home(self.home)['server'], self.server)

    def test_holder_creation_time_must_match_native_process(self):
        self.holders[0]['start'] = [201, 1]
        with self.assertRaises(ValueError):
            platform.desktop_for_home(self.home)

    def test_loaded_requires_exact_unique_writer_holder(self):
        evidence = platform.desktop_for_home(self.home)
        self.assertTrue(platform.loaded(self.thread, evidence))
        self.holders = []
        self.assertFalse(platform.loaded(self.thread, evidence))

    def test_loaded_multiple_or_foreign_writer_denied(self):
        evidence = platform.desktop_for_home(self.home)
        with mock.patch.object(platform, '_holders', side_effect=[self.holders, [dict(self.holders[0]), {'pid': 30, 'start': [300, 1]}]]):
            self.assertFalse(platform.loaded(self.thread, evidence))

    def test_native_process_reuse_invalidates_evidence(self):
        evidence = platform.desktop_for_home(self.home)
        self.identities[20] = dict(self.server, start=[201, 1])
        self.assertFalse(platform.revalidate(evidence))
        self.assertFalse(platform.loaded(self.thread, evidence))

    def test_hardlinked_database_denied(self):
        os.link(self.home / 'state_5.sqlite', self.home / 'duplicate.sqlite')
        with self.assertRaises(ValueError):
            platform.desktop_for_home(self.home)

    def test_hardlinked_writer_denied(self):
        evidence = platform.desktop_for_home(self.home)
        os.link(self.home / 'thread-writer-locks' / (self.thread + '.lock'), self.home / 'duplicate.lock')
        self.assertFalse(platform.loaded(self.thread, evidence))

    def test_invalid_thread_has_no_file_inventory(self):
        evidence = platform.desktop_for_home(self.home)
        with mock.patch.object(platform, '_holders') as inventory:
            self.assertFalse(platform.loaded('../invalid', evidence))
        inventory.assert_not_called()

    def test_unknown_os_and_missing_home_denied(self):
        with mock.patch.object(platform, '_platform', return_value='unknown'):
            with self.assertRaises(ValueError):
                platform.desktop_for_home(self.home)
        with self.assertRaises(ValueError):
            platform.desktop_for_home(self.home / 'missing')

    def test_revalidation_requires_same_home_files(self):
        evidence = platform.desktop_for_home(self.home)
        (self.home / 'state_6.sqlite').write_bytes(b'new generation')
        self.assertFalse(platform.revalidate(evidence))

    def test_codesign_failure_cannot_create_evidence(self):
        with mock.patch.object(platform, '_trusted_chain', side_effect=ValueError('verification unavailable')):
            with self.assertRaises(ValueError):
                platform.desktop_for_home(self.home)

    def test_writer_holder_must_still_match_after_inventory(self):
        evidence = platform.desktop_for_home(self.home)
        with mock.patch.object(platform, 'revalidate', side_effect=[True, False]):
            self.assertFalse(platform.loaded(self.thread, evidence))

    def test_server_chain_requires_parent_age_and_exact_programs(self):
        host = dict(self.server, pid=30, ppid=20, executable=str(self.home / 'codex-code-mode-host.exe'), start=[300, 1])
        self.identities[30] = host
        desktop, server, chain = platform._chain({'pid': 30, 'start': host['start']})
        self.assertEqual(server, self.server)
        self.assertEqual(desktop, self.desktop)
        self.assertEqual(len(chain), 3)
        self.identities[30] = dict(host, start=[99, 1])
        with self.assertRaises(ValueError):
            platform._chain({'pid': 30, 'start': [99, 1]})

    def test_darwin_requires_explicit_verified_team(self):
        with self.assertRaises(ValueError):
            platform._darwin_cli(self.desktop, self.server, self.chain, None)

    def test_darwin_bundle_and_code_signatures_match_exact_paths(self):
        bundle = self.home / 'Codex.app'
        macos = bundle / 'Contents' / 'MacOS'
        resources = bundle / 'Contents' / 'Resources'
        macos.mkdir(parents=True)
        resources.mkdir()
        desktop_path = macos / 'Codex'
        cli = resources / 'codex'
        desktop_path.write_bytes(b'executable')
        cli.write_bytes(b'executable')
        (bundle / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Codex'}))
        self.darwin_nested_seal(bundle, [cli])
        desktop = dict(self.desktop, executable=str(desktop_path))
        server = dict(self.server, executable=str(cli))
        with mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(platform._darwin_cli(desktop, server, [server, desktop], '2DC432GLL2'), str(cli))
            self.assertEqual(run.call_count, 2)
            requirement = run.call_args_list[0].args[0]
            self.assertTrue(requirement[4].startswith('='))
            self.assertTrue(all(call.args[0][4].startswith('=') for call in run.call_args_list))
            self.assertIn('com.openai.codex', requirement[4])
            self.assertIn('2DC432GLL2', requirement[4])
            run.return_value.returncode = 1
            with self.assertRaises(ValueError):
                platform._darwin_cli(desktop, server, [server, desktop], '2DC432GLL2')

    def darwin_nested_fixture(self):
        bundle = self.home / 'Codex.app'
        macos = bundle / 'Contents' / 'MacOS'
        native = bundle / 'Contents' / 'Resources' / 'codex-cli' / 'bin'
        macos.mkdir(parents=True)
        native.mkdir(parents=True)
        desktop_path = macos / 'Codex'
        cli = native / 'codex'
        host_path = native / 'codex-code-mode-host'
        for path in (desktop_path, cli, host_path):
            path.write_bytes(b'fixture executable')
        (bundle / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Codex'}))
        self.darwin_nested_seal(bundle, [cli, host_path])
        return (bundle, dict(self.desktop, executable=str(desktop_path)),
                dict(self.server, executable=str(cli)),
                dict(self.server, pid=30, ppid=20, executable=str(host_path), start=[300, 1]))

    def darwin_nested_seal(self, bundle, executables):
        directory = bundle / 'Contents' / '_CodeSignature'
        directory.mkdir(exist_ok=True)
        files = {path.relative_to(bundle / 'Contents').as_posix(): {'cdhash': b'x' * 20, 'requirement': 'anchor apple generic'}
                 for path in executables}
        (directory / 'CodeResources').write_bytes(plistlib.dumps({'files2': files}))

    def test_darwin_nested_official_cli_and_host_are_supported(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        self.assertEqual(platform.DARWIN_TEAM, '2DC432GLL2')
        with mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(platform._darwin_cli(desktop, server, [host, server, desktop], platform.DARWIN_TEAM), server['executable'])
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertIn('2DC432GLL2', call.args[0][4])

    def test_unsigned_darwin_cli_requires_exact_resource_seal_from_signed_bundle(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        signature = bundle / 'Contents' / '_CodeSignature'
        signature.mkdir(exist_ok=True)
        relative = pathlib.Path(server['executable']).relative_to(bundle / 'Contents').as_posix()
        (signature / 'CodeResources').write_bytes(plistlib.dumps({'files2': {relative: {'hash2': hashlib.sha256(pathlib.Path(server['executable']).read_bytes()).digest()}}}))
        with mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', side_effect=[types.SimpleNamespace(returncode=0), types.SimpleNamespace(returncode=1)]) as run:
            self.assertEqual(platform._darwin_cli(desktop, server, [server, desktop], platform.DARWIN_TEAM), server['executable'])
            run.assert_called_once()

    def test_invalid_resource_seal_cannot_fall_back_to_successful_individual_signature(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        path = bundle / 'Contents' / '_CodeSignature' / 'CodeResources'
        relative = pathlib.Path(server['executable']).relative_to(bundle / 'Contents').as_posix()
        for entry in ({'hash2': b'x' * 32}, {'hash2': hashlib.sha256(pathlib.Path(server['executable']).read_bytes()).digest(), 'optional': True}):
            path.write_bytes(plistlib.dumps({'files2': {relative: entry}}))
            with mock.patch.object(platform, 'validate_location', side_effect=lambda target: target), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
                with self.assertRaises(ValueError):
                    platform._darwin_cli(desktop, server, [server, desktop], platform.DARWIN_TEAM)
                run.assert_called_once()

    def test_unsigned_sealed_resource_still_requires_valid_deep_publisher_bundle_signature(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        path = bundle / 'Contents' / '_CodeSignature' / 'CodeResources'
        relative = pathlib.Path(server['executable']).relative_to(bundle / 'Contents').as_posix()
        path.write_bytes(plistlib.dumps({'files2': {relative: {'hash2': hashlib.sha256(pathlib.Path(server['executable']).read_bytes()).digest()}}}))
        with mock.patch.object(platform, 'validate_location', side_effect=lambda target: target), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)) as run:
            with self.assertRaises(ValueError):
                platform._darwin_cli(desktop, server, [server, desktop], platform.DARWIN_TEAM)
            run.assert_called_once()
            arguments = run.call_args.args[0]
            self.assertIn('--deep', arguments)
            self.assertIn('--strict', arguments)
            self.assertIn('anchor apple generic', arguments[4])
            self.assertIn('com.openai.codex', arguments[4])
            self.assertIn('2DC432GLL2', arguments[4])

    def test_resource_envelope_changed_after_bundle_signature_is_rejected(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        path = bundle / 'Contents' / '_CodeSignature' / 'CodeResources'

        def changed_after_verification(*args, **kwargs):
            path.write_bytes(path.read_bytes() + b' ')
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(platform, 'validate_location', side_effect=lambda target: target), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', side_effect=changed_after_verification):
            with self.assertRaises(ValueError):
                platform._darwin_cli(desktop, server, [server, desktop], platform.DARWIN_TEAM)

    def test_darwin_nested_paths_cannot_escape_signed_contents(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        invalid = (self.home / 'codex', bundle / 'Contents-copy' / 'codex',
                   bundle / 'Contents' / '..' / 'codex',
                   bundle / 'Contents' / 'Resources' / '..' / 'Resources' / 'codex')
        with mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)):
            for path in invalid:
                with self.subTest(path_kind=path.name):
                    changed = dict(server, executable=str(path))
                    with self.assertRaises(ValueError):
                        platform._darwin_cli(desktop, changed, [changed, desktop], platform.DARWIN_TEAM)

    def test_darwin_nested_cli_rejects_foreign_publisher_or_signature(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        with mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)):
            with self.assertRaises(ValueError):
                platform._darwin_cli(desktop, server, [server, desktop], 'AAAAAAAAAA')
        with mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', side_effect=[types.SimpleNamespace(returncode=0), types.SimpleNamespace(returncode=1)]):
            with self.assertRaises(ValueError):
                platform._darwin_cli(desktop, server, [server, desktop], platform.DARWIN_TEAM)

    def test_darwin_nested_cli_rejects_linked_components_before_acl(self):
        bundle, desktop, server, host = self.darwin_nested_fixture()
        actual = platform.reject_links

        def refuse_native(path):
            if pathlib.Path(path) == pathlib.Path(server['executable']):
                raise RuntimeError('linked component')
            return actual(path)

        with mock.patch.object(platform, 'reject_links', side_effect=refuse_native), mock.patch.object(platform, 'validate_location', side_effect=lambda path: path), mock.patch('desktop_binding.desktop_bundle', return_value='com.openai.codex'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)):
            with self.assertRaises((ValueError, RuntimeError)):
                platform._darwin_cli(desktop, server, [server, desktop], platform.DARWIN_TEAM)

    def test_restart_manager_inventory_releases_session_on_error(self):
        native = types.SimpleNamespace(**{name: mock.Mock(return_value=0) for name in ('RmStartSession', 'RmRegisterResources', 'RmGetList', 'RmEndSession')})
        native.RmRegisterResources.return_value = 5
        with mock.patch.object(platform.ctypes, 'WinDLL', return_value=native, create=True):
            with self.assertRaises(ValueError):
                platform._windows_holders([self.home / 'queue_1.sqlite'])
        native.RmEndSession.assert_called_once()
        self.assertEqual(set(vars(native)), {'RmStartSession', 'RmRegisterResources', 'RmGetList', 'RmEndSession'})

    def test_restart_manager_reads_creation_time_without_locking(self):
        native = types.SimpleNamespace(**{name: mock.Mock(return_value=0) for name in ('RmStartSession', 'RmRegisterResources', 'RmGetList', 'RmEndSession')})

        def inventory(session, needed, count, rows, reasons):
            if rows is None:
                needed._obj.value = 1
                return 234
            rows[0].identity.pid = 20
            rows[0].identity.created.dwLowDateTime = 200
            rows[0].identity.created.dwHighDateTime = 1
            count._obj.value = 1
            return 0

        native.RmGetList.side_effect = inventory
        with mock.patch.object(platform.ctypes, 'WinDLL', return_value=native, create=True):
            self.assertEqual(platform._windows_holders([self.home / 'queue_1.sqlite']), self.holders)
        native.RmEndSession.assert_called_once()

    def test_windows_native_cache_uses_official_verified_staging_gate(self):
        root = self.home / 'package'
        desktop = dict(self.desktop, executable=str(root / 'app' / 'ChatGPT.exe'))
        server = dict(self.server, executable=str(self.home / 'native-cache' / 'codex.exe'))
        staged = self.home / 'protected-cache' / 'codex.exe'

        def location(path):
            if path != staged:
                raise ValueError('untrusted direct location')
            return staged

        with mock.patch('windows_package.verify_package', return_value={'root': str(root)}), mock.patch('cli_security._system_powershell', return_value=self.home / 'powershell.exe'), mock.patch.object(platform.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=b'true')), mock.patch.object(platform, '_regular', return_value=[1, 2]), mock.patch('windows_runtime.stage_cli', return_value=staged) as stage, mock.patch.object(platform, 'validate_location', side_effect=location):
            self.assertEqual(platform._windows_cli(desktop, server, [server, desktop]), str(staged))
            stage.assert_called_once_with(root, server['executable'])
            stage.side_effect = ValueError('official bytes mismatch')
            with self.assertRaises(ValueError):
                platform._windows_cli(desktop, server, [server, desktop])


class DarwinBackendSelectionTests(unittest.TestCase):
    def setUp(self):
        root = pathlib.Path.cwd()
        self.desktop = {'pid': 10, 'ppid': 1, 'start': [100, 0],
                        'executable': str(root / 'Codex.app/Contents/MacOS/Codex')}
        self.server = {'pid': 20, 'ppid': 10, 'start': [200, 0],
                       'executable': str(root / 'Codex.app/Contents/Resources/codex')}
        self.gui = {'pid': 30, 'ppid': 1, 'start': [110, 0],
                    'executable': str(root / 'CodexSafeResume.app/Contents/MacOS/CodexSafeResume')}
        self.helper = {'pid': 40, 'ppid': 30, 'start': [210, 0], 'executable': self.server['executable']}
        self.identities = {row['pid']: row for row in (self.desktop, self.server, self.gui, self.helper)}
        self.holders = [{'pid': 20, 'start': [200, 0]}, {'pid': 40, 'start': [210, 0]}]
        self.bundles = {10: 'com.openai.codex', 30: 'com.codexsaferesume.app'}
        import desktop_binding
        patches = [mock.patch.object(platform, '_platform', return_value='darwin'),
                   mock.patch.object(platform, '_identity', side_effect=lambda pid: dict(self.identities[pid])),
                   mock.patch.object(platform, '_server_role', return_value='app-server'),
                   mock.patch.object(desktop_binding, 'desktop_bundle', side_effect=lambda pid: self.bundles[pid])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_installed_safe_resume_helper_is_not_a_second_desktop(self):
        self.assertEqual(platform._backend_holder(self.holders), self.server)

    def test_python_parent_and_proven_other_application_are_excluded(self):
        self.identities[30] = dict(self.gui, executable=str(pathlib.Path.cwd() / 'python3'))
        self.assertEqual(platform._backend_holder(self.holders), self.server)
        self.identities[30] = dict(self.gui)
        self.bundles[30] = 'com.apple.Terminal'
        self.assertEqual(platform._backend_holder(self.holders), self.server)

    def test_two_official_backend_holders_are_still_rejected(self):
        self.bundles[30] = 'com.openai.codex'
        with self.assertRaises(ValueError):
            platform._backend_holder(self.holders)

    def test_missing_unreadable_or_malformed_bundle_identity_is_not_skipped(self):
        import desktop_binding
        for value in (None, '', 'None', 123, True, b'com.codexsaferesume.app', 'not-a-bundle',
                      'com.example bad', 'com.example\0app', 'a.' + 'b' * 256):
            with self.subTest(value=value):
                self.bundles[30] = value
                with self.assertRaises(ValueError):
                    platform._backend_holder(self.holders)
        with mock.patch.object(desktop_binding, 'desktop_bundle', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError):
                platform._backend_holder(self.holders)

    def test_parent_pid_reuse_or_path_change_during_bundle_lookup_is_rejected(self):
        import desktop_binding
        for change in ({'start': [111, 0]}, {'executable': self.desktop['executable']}):
            def bundle(pid):
                if pid == 30:
                    self.identities[30] = dict(self.gui, **change)
                return self.bundles[pid]
            self.identities[30] = dict(self.gui)
            with mock.patch.object(desktop_binding, 'desktop_bundle', side_effect=bundle):
                with self.assertRaises(ValueError):
                    platform._backend_holder(self.holders)

    def test_unreadable_helper_or_parent_remains_a_denial(self):
        for missing in (30, 40):
            def identity(pid):
                if pid == missing:
                    raise ValueError('unavailable')
                return dict(self.identities[pid])
            with mock.patch.object(platform, '_identity', side_effect=identity):
                with self.assertRaises(ValueError):
                    platform._backend_holder(self.holders)


class QueueAttestationTests(unittest.TestCase):
    def setUp(self):
        import queue_attestation
        self.attestation = queue_attestation
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name).resolve()
        self.file = self.root / 'codex'
        self.file.write_bytes(b'original signed bytes')
        patch = mock.patch.object(self.attestation, '_security', return_value=b'owner-and-acl')
        patch.start()
        self.addCleanup(patch.stop)

    def test_unchanged_attestation_is_read_only(self):
        proof = self.attestation.capture([self.file], exact_directories=[self.root])
        with mock.patch.object(pathlib.Path, 'mkdir', side_effect=AssertionError('write')):
            self.assertTrue(self.attestation.matches(proof))

    def test_unrelated_ancestor_sibling_directory_addition_and_removal_are_allowed(self):
        target = self.root / 'target'
        target.mkdir()
        executable = target / 'codex'
        executable.write_bytes(b'original signed bytes')
        sibling = self.root / 'unrelated'
        original_lstat = pathlib.Path.lstat

        def lstat(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path == self.root:
                fields = {name: getattr(info, name) for name in dir(info)
                          if name.startswith('st_')}
                fields['st_nlink'] = 4 if sibling.exists() else 3
                return types.SimpleNamespace(**fields)
            return info

        with mock.patch.object(pathlib.Path, 'lstat', lstat):
            proof = self.attestation.capture([executable], tree=target)
            self.assertTrue(self.attestation.matches(proof))
            sibling.mkdir()
            self.assertTrue(self.attestation.matches(proof))
            sibling.rmdir()
            self.assertTrue(self.attestation.matches(proof))

    def test_added_directory_inside_sealed_tree_is_denied(self):
        proof = self.attestation.capture([self.file], tree=self.root)
        (self.root / 'injected').mkdir()
        self.assertFalse(self.attestation.matches(proof))

    def test_identical_directory_replacement_is_denied(self):
        target = self.root / 'target'
        target.mkdir()
        executable = target / 'codex'
        executable.write_bytes(b'original signed bytes')
        proof = self.attestation.capture([executable], tree=target)
        original_target = target.rename(self.root / 'original-target')
        target.mkdir()
        (original_target / 'codex').rename(executable)
        self.assertFalse(self.attestation.matches(proof))

    @unittest.skipIf(os.name == 'nt', 'POSIX directory mode changes')
    def test_ancestor_directory_mode_change_is_denied(self):
        proof = self.attestation.capture([self.file])
        before = self.root.stat().st_mode
        self.root.chmod(before ^ 0o020)
        self.addCleanup(self.root.chmod, before)
        self.assertFalse(self.attestation.matches(proof))

    def test_regular_file_hardlink_addition_is_denied(self):
        proof = self.attestation.capture([self.file])
        os.link(self.file, self.root / 'hardlink')
        try:
            matches = self.attestation.matches(proof)
        except ValueError:
            matches = False
        self.assertFalse(matches)
        with self.assertRaises(ValueError):
            self.attestation.capture([self.file])

    def test_changed_bytes_with_restored_size_and_mtime_are_denied(self):
        proof = self.attestation.capture([self.file])
        before = self.file.stat()
        self.file.write_bytes(b'changed! signed bytes')
        os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.file.stat().st_size, before.st_size)
        self.assertEqual(self.file.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.attestation.matches(proof))

    def test_identical_replacement_with_different_file_identity_is_denied(self):
        proof = self.attestation.capture([self.file])
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.file.read_bytes())
        replacement.replace(self.file)
        self.assertFalse(self.attestation.matches(proof))

    def test_acl_or_owner_descriptor_change_is_denied(self):
        proof = self.attestation.capture([self.file])
        with mock.patch.object(self.attestation, '_security', return_value=b'changed-owner-or-acl'):
            self.assertFalse(self.attestation.matches(proof))

    def test_ancestor_permission_descriptor_change_is_denied(self):
        proof = self.attestation.capture([self.file])
        with mock.patch.object(self.attestation, '_security', side_effect=lambda path: b'changed' if path == self.root else b'owner-and-acl'):
            self.assertFalse(self.attestation.matches(proof))

    def test_added_staged_runtime_file_is_denied(self):
        proof = self.attestation.capture([self.file], exact_directories=[self.root])
        (self.root / 'injected.dll').write_bytes(b'foreign')
        self.assertFalse(self.attestation.matches(proof))

    def test_sealed_bundle_resource_changes_and_additions_are_denied(self):
        nested = self.root / 'Contents'
        nested.mkdir()
        resource = nested / 'resource'
        resource.write_bytes(b'sealed resource')
        proof = self.attestation.capture([self.file], tree=self.root)
        self.assertTrue(self.attestation.matches(proof))
        resource.write_bytes(b'changed resource')
        self.assertFalse(self.attestation.matches(proof))
        resource.write_bytes(b'sealed resource')
        (nested / 'added').write_bytes(b'new')
        self.assertFalse(self.attestation.matches(proof))

    def test_missing_permissions_or_deleted_file_fail_closed(self):
        proof = self.attestation.capture([self.file])
        with mock.patch.object(self.attestation, '_security', side_effect=OSError('unreadable')):
            with self.assertRaises(OSError):
                self.attestation.matches(proof)
        self.file.unlink()
        try:
            matches = self.attestation.matches(proof)
        except OSError:
            matches = False
        self.assertFalse(matches)

    def test_full_signature_is_bracketed_by_identical_content_attestations(self):
        desktop = dict(pid=10, ppid=1, start=[1, 0], executable=str(self.file))
        server = dict(pid=20, ppid=10, start=[2, 0], executable=str(self.file))
        identities = {10: desktop, 20: server}
        before = dict(files=dict(paths={str(self.file): ('trusted',)}), package={'full_name': 'v1'})
        after = dict(files=dict(paths={str(self.file): ('trusted',)}), package={'full_name': 'v1'})
        with mock.patch.object(platform, '_platform', return_value='win32'), mock.patch.object(platform, '_chain', return_value=(desktop, server, [server, desktop])), mock.patch.object(platform, '_identity', side_effect=lambda pid: identities[pid]), mock.patch.object(platform, '_attest_source', return_value=before), mock.patch.object(platform, '_attest_files', return_value=after), mock.patch.object(platform, '_windows_cli', return_value=str(self.file)) as verify:
            self.assertEqual(platform._trusted_chain({'pid': 20}, platform.DARWIN_TEAM)['attestation'], after)
            verify.assert_called_once()
            after['files']['paths'][str(self.file)] = ('changed-after-verification',)
            with self.assertRaises(ValueError):
                platform._trusted_chain({'pid': 20}, platform.DARWIN_TEAM)

    def test_darwin_signature_is_bracketed_without_accepting_changed_bundle(self):
        desktop = dict(pid=10, ppid=1, start=[1, 0], executable=str(self.file))
        server = dict(pid=20, ppid=10, start=[2, 0], executable=str(self.file))
        identities = {10: desktop, 20: server}
        before = dict(files=dict(paths={str(self.file): ('sealed',)}), package=None)
        after = dict(files=dict(paths={str(self.file): ('altered',)}), package=None)
        with mock.patch.object(platform, '_platform', return_value='darwin'), mock.patch.object(platform, '_chain', return_value=(desktop, server, [server, desktop])), mock.patch.object(platform, '_identity', side_effect=lambda pid: identities[pid]), mock.patch.object(platform, '_attest_source', return_value=before), mock.patch.object(platform, '_attest_files', return_value=after), mock.patch.object(platform, '_darwin_cli', return_value=str(self.file)) as verify:
            with self.assertRaises(ValueError):
                platform._trusted_chain({'pid': 20}, platform.DARWIN_TEAM)
            verify.assert_called_once()

    def test_windows_package_identity_change_invalidates_cached_signature(self):
        evidence = {'desktop': {'pid': 10}, 'attestation': {'package': {'full_name': 'v1'}, 'files': {}}}
        with mock.patch.object(platform, '_platform', return_value='win32'), mock.patch.object(platform, '_windows_package_binding', return_value={'full_name': 'v2'}), mock.patch.object(self.attestation, 'matches') as match:
            self.assertFalse(platform._attested_files_match(evidence))
            match.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native descriptor')
    def test_native_windows_security_descriptor_is_stable_and_changes_with_file(self):
        first = self.attestation._windows_security(self.file)
        self.assertTrue(first)
        self.assertEqual(first, self.attestation._windows_security(self.file))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows native descriptor')
    def test_native_windows_file_and_all_ancestor_descriptors_are_stable(self):
        with mock.patch.object(self.attestation, '_security', side_effect=self.attestation._windows_security):
            proof = self.attestation.capture([self.file])
            self.assertTrue(self.attestation.matches(proof))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows synthesized executable mode')
    def test_windows_extension_execute_bits_do_not_conflict_with_file_handle_identity(self):
        executable = self.root / 'codex.exe'
        executable.write_bytes(b'official fixture')
        proof = self.attestation.capture([executable])
        self.assertTrue(self.attestation.matches(proof))

    def test_verified_package_boundary_does_not_request_outer_parent_acl(self):
        outer = self.root / 'WindowsApps'
        package = outer / 'signed-package'
        package.mkdir(parents=True)
        binary = package / 'codex.exe'
        binary.write_bytes(b'signed official CLI')
        with mock.patch.object(self.attestation, '_security', side_effect=lambda path: (_ for _ in ()).throw(PermissionError()) if path == outer else b'acl'):
            proof = self.attestation.capture([binary], package_boundary=package)
            self.assertTrue(self.attestation.matches(proof))
            self.assertNotIn(str(outer), proof['paths'])
            self.assertIn(str(package), proof['paths'])

    def test_native_darwin_missing_extended_acl_requires_existing_object(self):
        native = types.SimpleNamespace(**{name: mock.Mock(return_value=0) for name in ('acl_get_link_np', 'acl_to_text', 'acl_free')})

        def absent(*args):
            ctypes.set_errno(errno.ENOENT)
            return None

        native.acl_get_link_np.side_effect = absent
        with mock.patch.object(self.attestation.ctypes, 'CDLL', return_value=native):
            self.assertEqual(self.attestation._darwin_security(self.file), b'no-extended-acl')
            with self.assertRaises(OSError):
                self.attestation._darwin_security(self.root / 'missing')
        native.acl_free.assert_not_called()

    def test_native_darwin_acl_access_denied_and_unknown_errors_fail_closed(self):
        native = types.SimpleNamespace(**{name: mock.Mock(return_value=0) for name in ('acl_get_link_np', 'acl_to_text', 'acl_free')})
        for error in (0, errno.EACCES, errno.ENOTSUP):
            def unavailable(*args):
                ctypes.set_errno(error)
                return None

            native.acl_get_link_np.side_effect = unavailable
            with mock.patch.object(self.attestation.ctypes, 'CDLL', return_value=native):
                with self.assertRaises(ValueError):
                    self.attestation._darwin_security(self.file)


if __name__ == '__main__':
    unittest.main()
