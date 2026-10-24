"""CLIを実行せず、配置の権限とACL照合の失敗時停止を検査する。"""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_security


class CliLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.child = self.root / 'protected'
        self.child.mkdir(mode=0o700)
        self.cli = self.child / 'codex'
        self.cli.write_bytes(b'not executable content; never run')
        self.cli.chmod(0o700)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_accepts_protected_file(self):
        self.assertEqual(cli_security.validate_location(self.cli), self.cli)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_darwin_admin_exception_is_directory_root_group_only(self):
        fixture = Path('/trusted/cli')
        def check(uid, gid, mode):
            def info(path, *args, **kwargs):
                if path == fixture.parent:
                    return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)
                return SimpleNamespace(st_uid=0, st_gid=0, st_nlink=1,
                                       st_mode=(stat.S_IFREG if path == fixture else stat.S_IFDIR) | 0o755)
            with patch('cli_security.sys.platform', 'darwin'), \
                    patch('grp.getgrnam', return_value=SimpleNamespace(gr_gid=12345)), \
                    patch.object(Path, 'stat', info):
                cli_security._posix_location(fixture)
        check(0, 12345, stat.S_IFDIR | 0o775)
        for uid, gid, mode in ((99999, 12345, stat.S_IFDIR | 0o775),
                               (0, 99999, stat.S_IFDIR | 0o775),
                               (0, 12345, stat.S_IFDIR | 0o777)):
            with self.subTest(uid=uid, gid=gid, mode=mode):
                with self.assertRaises(ValueError): check(uid, gid, mode)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_accepts_root_owned_sticky_system_temp_with_protected_child(self):
        parent = Path('/tmp').resolve()
        info = parent.stat()
        if info.st_uid != 0 or not info.st_mode & stat.S_ISVTX:
            self.skipTest('root-owned sticky temp ancestor is unavailable')
        with tempfile.TemporaryDirectory(dir=parent) as name:
            fixture = Path(name) / 'codex'
            fixture.write_bytes(b'never executed')
            fixture.chmod(0o700)
            self.assertEqual(cli_security.validate_location(fixture), fixture)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_rejects_real_nonsticky_writable_ancestor(self):
        for mode in (0o777, 0o775, 0o773):
            with self.subTest(mode=mode):
                self.root.chmod(mode)
                try:
                    with self.assertRaises(ValueError):
                        cli_security.validate_location(self.cli)
                finally:
                    self.root.chmod(0o700)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_user_owned_sticky_parent_is_not_a_root_sticky_exception(self):
        if os.getuid() == 0:
            self.skipTest('requires non-root test user')
        self.root.chmod(0o1777)
        try:
            with self.assertRaises(ValueError):
                cli_security.validate_location(self.cli)
        finally:
            self.root.chmod(0o700)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_rejects_writable_or_nonexecutable_file(self):
        for mode in (0o722, 0o770, 0o600):
            with self.subTest(mode=mode):
                self.cli.chmod(mode)
                with self.assertRaises(ValueError):
                    cli_security.validate_location(self.cli)

    def test_rejects_relative_missing_directory_and_symlink(self):
        for invalid in ('codex', self.root / 'missing', self.child):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    cli_security.validate_location(invalid)
        link = self.child / 'link'
        try:
            link.symlink_to(self.cli)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(ValueError):
            cli_security.validate_location(link)
        parent_link = self.root / 'alias'
        parent_link.symlink_to(self.child, target_is_directory=True)
        with self.assertRaises(ValueError):
            cli_security.validate_location(parent_link / 'codex')

    @unittest.skipUnless(os.name == 'posix', 'POSIX permission fixture')
    def test_rejects_hardlinked_file(self):
        os.link(self.cli, self.child / 'other')
        with self.assertRaises(ValueError):
            cli_security.validate_location(self.cli)


class DarwinAclTests(unittest.TestCase):
    def setUp(self):
        self.path = Path('/fixture/quote-\'-$-ねく/codex')
        self.header = '-rwx------  1 owner  group  5 Oct 18 00:00 fixture\n'

    def result(self, acl='', code=0, error=''):
        header = self.header
        if acl:
            header = header[:10] + '+' + header[10:]
        return subprocess.CompletedProcess([], code, stdout=header+acl, stderr=error)

    def test_no_acl_and_normal_deny_delete_are_allowed(self):
        for acl in ('', ' 0: group:everyone deny delete\n',
                    ' 0: user:reader allow read,execute,readattr\n'):
            with self.subTest(acl=acl):
                with patch('cli_security.subprocess.run', return_value=self.result(acl)):
                    cli_security._darwin_acl(self.path)

    def test_each_dangerous_allow_permission_is_rejected(self):
        for right in ('write', 'append', 'add_file', 'add_subdirectory', 'delete',
                      'delete_child', 'writeattr', 'writeextattr', 'writesecurity', 'chown'):
            with self.subTest(right=right):
                response = self.result(' 0: user:owner allow '+right+'\n')
                with patch('cli_security.subprocess.run', return_value=response):
                    with self.assertRaises(ValueError):
                        cli_security._darwin_acl(self.path)

    def test_malformed_unknown_incomplete_and_failure_stop(self):
        for acl in (' 0: group:everyone unexpected write\n', ' 0: malformed\n',
                    ' 0: group:everyone allow mystery_right\n',
                    ' 0: group:everyone deny mystery_right\n', 'unexpected extra line\n'):
            with self.subTest(acl=acl):
                with patch('cli_security.subprocess.run', return_value=self.result(acl)):
                    with self.assertRaises(ValueError):
                        cli_security._darwin_acl(self.path)
        for result in (self.result(code=1), self.result(error='failure'),
                       subprocess.CompletedProcess([], 0, stdout='', stderr=''),
                       subprocess.CompletedProcess([], 0, stdout=self.header[:10]+'+\n', stderr='')):
            with patch('cli_security.subprocess.run', return_value=result):
                with self.assertRaises(ValueError):
                    cli_security._darwin_acl(self.path)
        for failure in (subprocess.TimeoutExpired('/bin/ls', 3), OSError('fixture')):
            with patch('cli_security.subprocess.run', side_effect=failure):
                with self.assertRaises(ValueError):
                    cli_security._darwin_acl(self.path)

    def test_fixed_command_locale_and_timeout_without_shell_interpolation(self):
        with patch('cli_security.subprocess.run', return_value=self.result()) as run:
            cli_security._darwin_acl(self.path)
        args, options = run.call_args
        self.assertEqual(args[0], ['/bin/ls', '-lde', '--', str(self.path)])
        self.assertFalse(options['shell'])
        self.assertEqual(options['env'], dict(os.environ, LC_ALL='C'))
        self.assertEqual(options['stdout'], subprocess.PIPE)
        self.assertEqual(options['stderr'], subprocess.PIPE)
        self.assertGreater(options['timeout'], 0)
        self.assertLessEqual(options['timeout'], 3)

    @unittest.skipUnless(os.name == 'posix', 'POSIX location fixture')
    def test_darwin_checks_file_and_every_ancestor_after_modes(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name).resolve() / 'codex'
            path.write_bytes(b'never executed')
            path.chmod(0o700)
            with patch('cli_security.sys.platform', 'darwin'):
                with patch('cli_security._darwin_acl') as acl:
                    cli_security.validate_location(path)
            self.assertEqual([call.args[0] for call in acl.call_args_list],
                             [path, *path.parents])

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS system alias fixture')
    def test_existing_system_temp_alias_uses_physical_ancestor_acl(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as name:
            path = Path(name) / 'codex'
            path.write_bytes(b'never executed')
            path.chmod(0o700)
            self.assertEqual(cli_security.validate_location(path), path)

    @unittest.skipUnless(sys.platform == 'darwin', 'native macOS ls fixture')
    def test_actual_native_ls_reads_fixture_without_acl_mutation(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name).resolve() / 'fixture'
            path.write_bytes(b'never executed')
            cli_security._darwin_acl(path)


class WindowsAclInvocationTests(unittest.TestCase):
    def setUp(self):
        self.path = Path('/fixture/quote-\'-$-unicode-ねく.exe')
        self.powershell = Path('/system/WindowsPowerShell/v1.0/powershell.exe')
        self.resolver = patch('cli_security._system_powershell', return_value=self.powershell)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)

    def test_constant_script_path_via_environment_and_command_shape(self):
        result = subprocess.CompletedProcess([], 0, stdout=b'true')
        with patch('cli_security.subprocess.run', return_value=result) as run:
            cli_security._windows_location(self.path)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(self.powershell), '-NoProfile', '-NonInteractive',
                                  '-Command', cli_security._ACL_SCRIPT])
        self.assertNotIn(str(self.path), args[0][-1])
        self.assertEqual(kwargs['env']['CSR_CLI_TO_VERIFY'], str(self.path))
        expected = dict(os.environ, CSR_CLI_TO_VERIFY=str(self.path))
        self.assertEqual(kwargs['env'], expected)
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertGreater(kwargs['timeout'], 0)
        self.assertLessEqual(kwargs['timeout'], 10)

    def test_false_malformed_nonzero_timeout_and_missing_shell_reject(self):
        for output, code in ((b'false', 0), (b'True', 0), (b'true\nnoise', 0),
                             (b'', 0), (b' true ', 0), (b'true', 1)):
            with self.subTest(output=output, code=code):
                result = subprocess.CompletedProcess([], code, stdout=output)
                with patch('cli_security.subprocess.run', return_value=result):
                    with self.assertRaises(ValueError) as caught:
                        cli_security._windows_location(self.path)
                self.assertNotIn(str(self.path), str(caught.exception))
        for failure in (subprocess.TimeoutExpired('fixture', 5), OSError('private path')):
            with patch('cli_security.subprocess.run', side_effect=failure):
                with self.assertRaises(ValueError):
                    cli_security._windows_location(self.path)
        with patch('cli_security._system_powershell', side_effect=OSError('fixture')):
            with self.assertRaises(ValueError):
                cli_security._windows_location(self.path)


class SystemPowerShellResolutionTests(unittest.TestCase):
    def test_uses_kernel_system_directory_without_path_lookup(self):
        with tempfile.TemporaryDirectory() as name:
            system = Path(name).resolve()
            kernel = Mock()
            def system_directory(buffer, size):
                buffer.value = str(system)
                return len(buffer.value)
            kernel.GetSystemDirectoryW.side_effect = system_directory
            with patch('cli_security.ctypes.WinDLL', return_value=kernel, create=True) as load:
                with patch('cli_security.Path.is_file', return_value=True):
                    result = cli_security._system_powershell()
            self.assertEqual(result, system / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
            load.assert_called_once_with('kernel32', use_last_error=True)
            self.assertTrue(kernel.GetSystemDirectoryW.argtypes)

    def test_native_failure_and_missing_shell_fail_closed(self):
        for length in (0, 32768):
            with self.subTest(length=length):
                kernel = Mock()
                kernel.GetSystemDirectoryW.return_value = length
                with patch('cli_security.ctypes.WinDLL', return_value=kernel, create=True):
                    with self.assertRaises(ValueError):
                        cli_security._system_powershell()
        with tempfile.TemporaryDirectory() as name:
            system = Path(name).resolve()
            kernel = Mock()
            def system_directory(buffer, size):
                buffer.value = str(system)
                return len(buffer.value)
            kernel.GetSystemDirectoryW.side_effect = system_directory
            with patch('cli_security.ctypes.WinDLL', return_value=kernel, create=True):
                with patch('cli_security.Path.is_file', return_value=False):
                    with self.assertRaises(ValueError):
                        cli_security._system_powershell()


@unittest.skipUnless(os.name == 'nt', 'Windows native ACL fixture requires Windows')
class WindowsNativeAclTests(unittest.TestCase):
    def test_actual_get_acl_accepts_protected_fixture_and_rejects_public_write(self):
        # Prepare only an isolated test tree; no application/CLI is executed.
        with tempfile.TemporaryDirectory() as name:
            fixture = Path(name) / 'fixture.exe'
            fixture.write_bytes(b'not a program')
            # 初期ACLで受入れを確認してから、隔離fixtureのDACLだけを変更する。
            # Set-Aclはownerを書き戻す場合があるため、既存のsystem directory
            # resolverから得たicaclsを使い、所有者・継承ACLには触れない。
            cli_security.validate_location(fixture)
            icacls = cli_security._system_powershell().parents[2] / 'icacls.exe'
            self.assertTrue(icacls.is_file(), 'system icacls is required for the native fixture')
            owner_rights = subprocess.run(
                [str(icacls), str(fixture), '/grant', '*S-1-3-4:(F)'],
                shell=False, capture_output=True, timeout=8)
            self.assertEqual(owner_rights.returncode, 0,
                             (owner_rights.stdout, owner_rights.stderr))
            # OWNER RIGHTS applies only to the object's independently trusted
            # owner. Other principals must still fail the same validation.
            cli_security.validate_location(fixture)
            result = subprocess.run(
                [str(icacls), str(fixture), '/grant', '*S-1-1-0:(WD)'],
                shell=False, capture_output=True, timeout=8)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            with self.assertRaises(ValueError):
                cli_security.validate_location(fixture)


if __name__ == '__main__':
    unittest.main()
