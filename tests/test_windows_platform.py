from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_platform as platform


@contextmanager
def unix_socket_fixture(root):
    winsock = ctypes.WinDLL('ws2_32', use_last_error=True)
    winsock.WSAStartup.argtypes = [ctypes.c_ushort, ctypes.c_void_p]
    winsock.WSAStartup.restype = ctypes.c_int
    winsock.socket.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    winsock.socket.restype = ctypes.c_size_t
    winsock.bind.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
    winsock.bind.restype = ctypes.c_int
    winsock.closesocket.argtypes = [ctypes.c_size_t]
    winsock.closesocket.restype = ctypes.c_int
    if winsock.WSAStartup(0x202, ctypes.create_string_buffer(512)):
        raise AssertionError('fixture socket startup failed')
    handle = winsock.socket(1, 1, 0)
    if handle == ctypes.c_size_t(-1).value:
        winsock.WSACleanup()
        raise AssertionError('fixture socket creation failed')
    previous = Path.cwd()
    try:
        os.chdir(root)
        address = ctypes.create_string_buffer(b'\x01\x00s\x00')
        if winsock.bind(handle, address, 4):
            raise AssertionError('fixture socket bind failed')
        os.chdir(previous)
        path = root / 's'
        subprocess.run(
            [str(platform.cli_security._system_powershell()), '-NoProfile', '-NonInteractive',
             '-Command', r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetOwner($me)
$acl.SetAccessRuleProtection($true, $false)
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $me, [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow))
Microsoft.PowerShell.Security\Set-Acl -LiteralPath $env:CSR_TEST_SOCKET -AclObject $acl
"""],
            check=True, shell=False, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=dict(os.environ, CSR_TEST_SOCKET=str(path)))
        yield path
    finally:
        os.chdir(previous)
        winsock.closesocket(handle)
        winsock.WSACleanup()


class GuiLockNativeOpenTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        kernel = SimpleNamespace(CreateFileW=Mock(return_value=71), CloseHandle=Mock())
        info = SimpleNamespace(attributes=0, links=1, size_high=0, size_low=0)
        tag = SimpleNamespace(attributes=0, tag=0)
        crt = SimpleNamespace(open_osfhandle=Mock(return_value=19))
        with patch.object(platform, '_require_windows'), \
                patch.object(platform, 'reject_links'), \
                patch.object(platform, '_native', return_value=(kernel, None, None)), \
                patch.object(platform, '_file_info', return_value=(info, tag)), \
                patch.object(platform, 'os', SimpleNamespace(O_RDWR=2, O_BINARY=8, O_NOINHERIT=128)), \
                patch.dict(sys.modules, msvcrt=crt):
            yield kernel, info, tag, crt

    def test_native_open_preserves_exact_error_before_other_calls(self):
        for number in (32, 33, 5, 12345):
            error = OSError('isolated native open error')
            error.winerror = number
            for invalid in (None, 0, ctypes.c_void_p(-1).value):
                with self.subTest(number=number, invalid=invalid), self.fixture() as (kernel, _, _, crt):
                    kernel.CreateFileW.return_value = invalid
                    with patch.object(platform.ctypes, 'get_last_error', return_value=number, create=True) as last, \
                            patch.object(platform.ctypes, 'WinError', return_value=error, create=True) as winerror:
                        with self.assertRaises(OSError) as caught:
                            platform.open_gui_lock(Path('fixture.gui.lock'))
                    self.assertIs(caught.exception, error)
                    last.assert_called_once_with()
                    winerror.assert_called_once_with(number)
                    crt.open_osfhandle.assert_not_called()
                    kernel.CloseHandle.assert_not_called()

    def test_valid_handle_transfers_ownership_to_noninheritable_crt_fd(self):
        with self.fixture() as (kernel, _, _, crt):
            flags = SimpleNamespace(O_RDWR=2, O_BINARY=8, O_NOINHERIT=128)
            with patch.object(platform, 'os', flags):
                self.assertEqual(platform.open_gui_lock(Path('fixture.gui.lock')), 19)
            kernel.CreateFileW.assert_called_once_with('fixture.gui.lock', 0xC0000000, 3, None,
                                                       4, 0x00200000, None)
            crt.open_osfhandle.assert_called_once_with(71, 2 | 8 | 128)
            kernel.CloseHandle.assert_not_called()

    def test_malformed_native_metadata_closes_handle_without_transfer(self):
        for field, value in (('attributes', 0x400), ('attributes', 0x10), ('links', 0),
                             ('links', 2), ('size_high', 1), ('size_low', 2)):
            with self.subTest(field=field, value=value), self.fixture() as (kernel, info, _, crt):
                setattr(info, field, value)
                with self.assertRaises((OSError, RuntimeError)):
                    platform.open_gui_lock(Path('fixture.gui.lock'))
                kernel.CloseHandle.assert_called_once_with(71)
                crt.open_osfhandle.assert_not_called()
        with self.fixture() as (kernel, _, tag, crt):
            tag.attributes = 0x400
            with self.assertRaises((OSError, RuntimeError)):
                platform.open_gui_lock(Path('fixture.gui.lock'))
            kernel.CloseHandle.assert_called_once_with(71)
            crt.open_osfhandle.assert_not_called()

    def test_metadata_query_failure_closes_owned_handle(self):
        with self.fixture() as (kernel, _, _, crt), \
                patch.object(platform, '_file_info', side_effect=ValueError('isolated query failure')):
            with self.assertRaises(RuntimeError):
                platform.open_gui_lock(Path('fixture.gui.lock'))
            kernel.CloseHandle.assert_called_once_with(71)
            crt.open_osfhandle.assert_not_called()

    def test_conversion_error_cannot_be_mistaken_for_open_contention(self):
        error = OSError('isolated conversion error')
        error.winerror = 32
        with self.fixture() as (kernel, _, _, crt):
            crt.open_osfhandle.side_effect = error
            with self.assertRaises(RuntimeError) as caught:
                platform.open_gui_lock(Path('fixture.gui.lock'))
            self.assertIs(caught.exception.__cause__, error)
            kernel.CloseHandle.assert_called_once_with(71)


class PlatformContractTests(unittest.TestCase):
    def test_invalid_pid_is_sanitized(self):
        for pid in (None, True, '1', 0, -1, 2 ** 40):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                platform.process_identity(pid)

    def test_unknown_platform_fails_closed(self):
        with patch.object(platform.sys, 'platform', 'unknown'):
            for operation in (lambda: platform.process_identity(1),
                              lambda: platform.endpoint_identity('private-fixture'),
                              lambda: platform.quit_desktop('identity', 'private-fixture')):
                with self.assertRaises(ValueError) as error:
                    operation()
                self.assertNotIn('private-fixture', str(error.exception))

    def test_bundle_uses_same_process_identity(self):
        with patch.object(platform, 'process_identity', return_value={'executable': 'verified.exe'}):
            self.assertEqual(platform.desktop_bundle(3), 'verified.exe')

    def test_endpoint_acl_keeps_conservative_permissions(self):
        for rule in ('DiscretionaryAcl', 'AccessControlType]::Allow', 'WriteData',
                     'DeleteSubdirectoriesAndFiles', 'ChangePermissions', 'TakeOwnership',
                     'TrustedInstaller', 'S-1-3-4'):
            self.assertIn(rule, platform._ENDPOINT_ACL_SCRIPT)


@unittest.skipUnless(sys.platform == 'win32', 'Windows native APIs')
class NativePlatformTests(unittest.TestCase):
    def test_command_line_round_trip_preserves_exact_quoted_settings(self):
        arguments = ['C:\\Program Files\\fixture\\codex.exe', '-c',
                     'features.code_mode_host=true', 'app-server', '-c',
                     'model="fixture model"', 'C:\\fixture trailing\\']
        self.assertEqual(platform.command_line_arguments(subprocess.list2cmdline(arguments)), arguments)

    def test_command_line_parser_rejects_missing_nul_and_oversized_input(self):
        for command in (None, '', 'a\x00b', 'a' * 32768):
            with self.subTest(command_type=type(command).__name__), self.assertRaises(ValueError):
                platform.command_line_arguments(command)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_current_process_identity_is_stable(self):
        identity = platform.process_identity(os.getpid())
        self.assertEqual(identity, platform.process_identity(os.getpid()))
        self.assertEqual(identity['pid'], os.getpid())
        self.assertEqual(identity['ppid'], os.getppid())
        self.assertTrue(os.path.samefile(identity['executable'], sys._base_executable))
        self.assertEqual(len(identity['start']), 2)
        self.assertGreater(sum(identity['start']), 0)

    def test_missing_process_is_sanitized(self):
        with self.assertRaises(ValueError):
            platform.process_identity(0xfffffffc)

    def test_regular_file_and_directory_are_not_endpoints(self):
        path = self.root / 'ordinary'
        path.write_bytes(b'fixture')
        for invalid in (path, self.root, self.root / 'missing'):
            with self.subTest(kind=invalid.name), self.assertRaises(ValueError):
                platform.endpoint_identity(invalid)

    def test_real_unix_socket_identity_is_stable(self):
        with unix_socket_fixture(self.root) as path:
            first = platform.endpoint_identity(path)
            self.assertEqual(first, platform.endpoint_identity(path))
            self.assertGreater(first['inode'], 0)
            self.assertGreater(first['epoch'], 0)

    def test_socket_writable_by_everyone_is_rejected(self):
        with unix_socket_fixture(self.root) as path:
            platform.endpoint_identity(path)
            icacls = platform.cli_security._system_powershell().parents[2] / 'icacls.exe'
            subprocess.run(
                [str(icacls), str(path), '/grant', '*S-1-1-0:(WD)'],
                check=True, shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            with self.assertRaises(ValueError):
                platform.endpoint_identity(path)

    def test_endpoint_pins_ancestors_before_opening_socket(self):
        kernel = platform._native()[0]
        original = kernel.CreateFileW
        with unix_socket_fixture(self.root) as path, \
                patch.object(kernel, 'CreateFileW', side_effect=original) as opened:
            platform.endpoint_identity(path)
            paths = [Path(call.args[0]) for call in opened.call_args_list]
            self.assertEqual(paths, list(reversed((path, *path.parents))))
            self.assertTrue(all(call.args[2] & 4 == 0 for call in opened.call_args_list))

    def test_endpoint_acl_failure_is_not_ignored(self):
        with unix_socket_fixture(self.root) as path, \
                patch.object(platform, '_endpoint_acl', side_effect=ValueError('private-fixture')) as acl:
            with self.assertRaises(ValueError) as error:
                platform.endpoint_identity(path)
            acl.assert_called_once_with(path)
            self.assertNotIn('private-fixture', str(error.exception))

    def test_process_owner_mismatch_is_rejected(self):
        security = platform._native()[1]
        with patch.object(security, 'EqualSid', return_value=False), self.assertRaises(ValueError):
            platform.process_identity(os.getpid())

    def test_socket_parent_link_is_rejected(self):
        target = self.root / 'real'
        target.mkdir()
        alias = self.root / 'alias'
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('directory symlink privilege unavailable')
        with unix_socket_fixture(target), self.assertRaises(ValueError):
            platform.endpoint_identity(alias / 's')

    def test_quit_rejects_mismatched_identifier_without_windows_calls(self):
        with patch.object(platform, '_matching_processes') as processes:
            with self.assertRaises(ValueError):
                platform.quit_desktop('different', sys.executable)
            processes.assert_not_called()

    def test_quit_returns_without_spawning_when_no_matching_process(self):
        path = str(Path(sys.executable).resolve())
        with patch.object(platform, '_matching_processes', return_value=[]), \
                patch.object(platform, '_process') as process, \
                patch.object(platform.subprocess, 'run', side_effect=AssertionError('app input forbidden')), \
                patch.object(platform.subprocess, 'Popen', side_effect=AssertionError('app launch forbidden')):
            platform.quit_desktop(path, path)
            process.assert_not_called()

    @contextmanager
    def quit_fixture(self, *, changed=False, replacement=False, timeout=False,
                     wrong_path=False, failed_wait=False, events=None):
        path = str(Path(sys.executable).resolve())
        identity = dict(pid=3, ppid=1, start=[1, 2], executable=path)
        actual = dict(identity, start=[3, 4]) if changed else dict(identity)
        if wrong_path:
            actual['executable'] = str(self.root / 'unrelated.exe')
        released = Mock()

        @contextmanager
        def process(pid):
            try:
                if events is not None:
                    events.append(('entered', pid))
                yield 7, actual
            finally:
                released()

        kernel = SimpleNamespace(WaitForSingleObject=Mock(
            side_effect=[0xffffffff] if failed_wait else None if timeout else [258, 0], return_value=258))
        snapshots = [[identity], [dict(identity, start=[8, 9])] if replacement else [identity], []]
        with patch.object(platform, '_matching_processes', side_effect=snapshots), \
                patch.object(platform, '_process', side_effect=process), \
                patch.object(platform, '_native', return_value=(kernel, None, None)), \
                patch.object(platform.ctypes, 'WinDLL', side_effect=AssertionError('app APIs forbidden')), \
                patch.object(platform.subprocess, 'run', side_effect=AssertionError('app input forbidden')), \
                patch.object(platform.subprocess, 'Popen', side_effect=AssertionError('app launch forbidden')), \
                patch.object(platform.time, 'monotonic', side_effect=[0, 1]), \
                patch.object(platform.time, 'sleep'):
            yield path, kernel, released

    def test_quit_only_observes_original_process_until_user_exit(self):
        with self.quit_fixture() as (path, kernel, released):
            platform.quit_desktop(path, path)
            self.assertEqual(kernel.WaitForSingleObject.call_count, 2)
            self.assertEqual(set(vars(kernel)), {'WaitForSingleObject'})
            released.assert_called_once()

    def test_quit_rejects_pid_reuse_before_waiting(self):
        with self.quit_fixture(changed=True) as (path, kernel, released):
            with self.assertRaises(ValueError):
                platform.quit_desktop(path, path)
            kernel.WaitForSingleObject.assert_not_called()
            released.assert_called_once()

    def test_quit_rejects_changed_executable_before_waiting(self):
        with self.quit_fixture(wrong_path=True) as (path, kernel, released):
            with self.assertRaises(ValueError):
                platform.quit_desktop(path, path)
            kernel.WaitForSingleObject.assert_not_called()
            released.assert_called_once()

    def test_quit_detects_replacement_process(self):
        with self.quit_fixture(replacement=True) as (path, kernel, released):
            with self.assertRaises(ValueError):
                platform.quit_desktop(path, path)
            released.assert_called_once()

    def test_quit_times_out_without_app_control(self):
        with self.quit_fixture(timeout=True) as (path, kernel, released):
            with self.assertRaises(ValueError):
                platform.quit_desktop(path, path, timeout=.5)
            released.assert_called_once()

    def test_quit_rejects_native_wait_failure(self):
        with self.quit_fixture(failed_wait=True) as (path, kernel, released):
            with self.assertRaises(ValueError):
                platform.quit_desktop(path, path)
            released.assert_called_once()

    def test_quit_waits_for_all_original_processes_without_app_control(self):
        path = str(Path(sys.executable).resolve())
        original = [dict(pid=pid, ppid=1, start=[pid, 2], executable=path) for pid in (3, 4)]
        released = []

        @contextmanager
        def process(pid):
            try:
                yield pid + 10, original[pid - 3]
            finally:
                released.append(pid)

        kernel = SimpleNamespace(WaitForSingleObject=Mock(side_effect=[258, 258, 0, 258, 0, 0]))
        snapshots = [original, original, [original[1]], []]
        with patch.object(platform, '_matching_processes', side_effect=snapshots), \
                patch.object(platform, '_process', side_effect=process), \
                patch.object(platform, '_native', return_value=(kernel, None, None)), \
                patch.object(platform.ctypes, 'WinDLL', side_effect=AssertionError('app APIs forbidden')), \
                patch.object(platform.subprocess, 'run', side_effect=AssertionError('app input forbidden')), \
                patch.object(platform.subprocess, 'Popen', side_effect=AssertionError('app launch forbidden')), \
                patch.object(platform.time, 'monotonic', side_effect=[0, 1, 2]), \
                patch.object(platform.time, 'sleep') as sleep:
            platform.quit_desktop(path, path, timeout=180)
        self.assertEqual(released, [4, 3])
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(set(vars(kernel)), {'WaitForSingleObject'})

    def test_quit_rejects_desktop_started_after_initial_absence(self):
        path = str(Path(sys.executable).resolve())
        replacement = dict(pid=3, ppid=1, start=[1, 2], executable=path)
        with patch.object(platform, '_matching_processes', side_effect=[[], [replacement]]), \
                patch.object(platform, '_process') as process, self.assertRaises(ValueError):
            platform.quit_desktop(path, path)
        process.assert_not_called()

    def test_ready_notifies_once_after_pinned_live_identity_snapshot(self):
        events = []
        with self.quit_fixture(events=events) as (path, kernel, released):
            kernel.WaitForSingleObject.side_effect = [258, 258, 0]

            def ready():
                self.assertEqual(events, [('entered', 3)])
                self.assertEqual(platform._matching_processes.call_count, 2)
                self.assertEqual(kernel.WaitForSingleObject.call_count, 2)
                released.assert_not_called()
                events.append(('ready', 3))

            callback = Mock(side_effect=ready)
            platform.quit_desktop(path, path, on_ready=callback)
            callback.assert_called_once_with()
            released.assert_called_once()
        self.assertEqual(events, [('entered', 3), ('ready', 3)])

    def test_ready_does_not_notify_for_identity_mismatch_or_replacement(self):
        for options in ({'changed': True}, {'replacement': True}, {'wrong_path': True}):
            callback = Mock()
            with self.subTest(options=options), self.quit_fixture(**options) as (path, _, released):
                with self.assertRaises(ValueError):
                    platform.quit_desktop(path, path, on_ready=callback)
                callback.assert_not_called()
                released.assert_called_once()

    def test_ready_callback_failure_releases_pinned_handles(self):
        with self.quit_fixture() as (path, kernel, released):
            kernel.WaitForSingleObject.side_effect = [258, 258]
            callback = Mock(side_effect=Exception('private-fixture'))
            with self.assertRaises(ValueError) as error:
                platform.quit_desktop(path, path, on_ready=callback)
            self.assertNotIn('private-fixture', str(error.exception))
            callback.assert_called_once_with()
            released.assert_called_once()

    def test_ready_does_not_notify_when_original_processes_already_exited(self):
        with self.quit_fixture() as (path, kernel, released):
            kernel.WaitForSingleObject.side_effect = [0]
            platform._matching_processes.side_effect = [
                [dict(pid=3, ppid=1, start=[1, 2], executable=path)], []]
            callback = Mock()
            platform.quit_desktop(path, path, on_ready=callback)
            callback.assert_not_called()
            released.assert_called_once()

    def test_ready_does_not_notify_when_desktop_already_absent(self):
        path = str(Path(sys.executable).resolve())
        callback = Mock()
        with patch.object(platform, '_matching_processes', return_value=[]):
            platform.quit_desktop(path, path, on_ready=callback)
        callback.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'Windows native adapter')
class AuthorityWaitTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(sys.executable).resolve())
        self.root = self.identity(3, 1, 1)

    def identity(self, pid, ppid, start):
        return dict(pid=pid, ppid=ppid, start=[start, 2], executable=self.path)

    @contextmanager
    def simulation(self, snapshots, *, callback=None, timeout=False):
        state = {'index': -1}
        entries, releases = [], []

        def matching(executable):
            state['index'] = min(state['index'] + 1, len(snapshots) - 1)
            current = snapshots[state['index']]
            if isinstance(current, Exception):
                raise current
            return current

        @contextmanager
        def process(pid):
            identity = next(item for item in snapshots[state['index']] if item['pid'] == pid)
            entries.append(pid)
            try:
                yield pid, dict(identity)
            finally:
                releases.append(pid)

        def wait(handle, duration):
            current = snapshots[state['index']]
            return 258 if any(item['pid'] == handle for item in current) else 0

        ready = Mock(side_effect=callback)
        kernel = SimpleNamespace(WaitForSingleObject=Mock(side_effect=wait))
        with patch.object(platform, '_matching_processes', side_effect=matching), \
                patch.object(platform, '_process', side_effect=process), \
                patch.object(platform, '_native', return_value=(kernel, None, None)), \
                patch.object(platform.ctypes, 'WinDLL', side_effect=AssertionError('app APIs forbidden')), \
                patch.object(platform.subprocess, 'Popen', side_effect=AssertionError('launch forbidden')), \
                patch.object(platform.subprocess, 'run', side_effect=AssertionError('app input forbidden')), \
                patch.object(platform.time, 'sleep'), \
                patch.object(platform.time, 'monotonic', side_effect=iter(range(100))):
            yield ready, entries, releases

    def wait(self, ready, **changes):
        platform.quit_desktop(self.path, self.path, authority=self.root, on_ready=ready,
                              timeout=changes.get('timeout', 30))

    def test_descendant_churn_before_notice_does_not_cancel_ready(self):
        old = self.identity(4, 3, 2)
        new = self.identity(5, 3, 3)
        snapshots = [[self.root, old], [self.root, new], [new], [], []]
        with self.simulation(snapshots) as (ready, entries, releases):
            self.wait(ready)
        ready.assert_called_once_with()
        self.assertEqual(entries, [3, 4, 5])
        self.assertEqual(releases, [5, 4, 3])

    def test_transient_snapshot_error_requires_a_fresh_complete_snapshot(self):
        child = self.identity(4, 3, 2)
        snapshots = [[self.root], ValueError('transient-fixture'), [self.root, child], [], []]
        with self.simulation(snapshots) as (ready, entries, releases):
            self.wait(ready)
        ready.assert_called_once()
        self.assertEqual(entries, [3, 4])
        self.assertEqual(releases, [4, 3])

    def test_snapshot_failures_have_a_fixed_retry_limit_without_notification(self):
        with self.simulation([ValueError('private-fixture')]) as (ready, entries, _):
            with patch.object(platform, '_matching_processes', side_effect=ValueError('private-fixture')) as collector:
                with self.assertRaises(ValueError) as error:
                    self.wait(ready)
                self.assertEqual(collector.call_count, 3)
        ready.assert_not_called()
        self.assertEqual(entries, [])
        self.assertNotIn('private-fixture', str(error.exception))

    def test_descendant_created_after_notice_is_tracked_until_exit(self):
        child = self.identity(4, 3, 2)
        with self.simulation([[self.root], [self.root], [self.root, child], [child], [], []]) as result:
            ready, entries, releases = result
            self.wait(ready)
        ready.assert_called_once_with()
        self.assertEqual(entries, [3, 4])
        self.assertEqual(releases, [4, 3])

    def test_descendant_exited_before_pinning_is_not_treated_as_live(self):
        child = self.identity(4, 3, 2)
        with self.simulation([[self.root], [self.root], [self.root, child], [], []]) as result:
            ready, entries, releases = result
            original = platform._process

            @contextmanager
            def process(pid):
                if pid == child['pid']:
                    raise platform._ProcessExited(platform._ERROR)
                with original(pid) as value:
                    yield value

            with patch.object(platform, '_process', side_effect=process):
                self.wait(ready)
        ready.assert_called_once()
        self.assertEqual(entries, [3])
        self.assertEqual(releases, [3])

    def test_live_descendant_validation_failure_is_still_rejected(self):
        child = self.identity(4, 3, 2)
        with self.simulation([[self.root], [self.root], [self.root, child]]) as result:
            ready, _, releases = result
            original = platform._process

            @contextmanager
            def process(pid):
                if pid == child['pid']:
                    raise ValueError('private-fixture')
                with original(pid) as value:
                    yield value

            with patch.object(platform, '_process', side_effect=process), self.assertRaises(ValueError):
                self.wait(ready)
        ready.assert_called_once()
        self.assertEqual(releases, [3])

    def test_native_exit_classification_requires_a_signaled_handle(self):
        for state in (0, 258, 0xffffffff):
            kernel = SimpleNamespace(WaitForSingleObject=Mock(return_value=state))
            with self.subTest(state=state), \
                    patch.object(platform, '_native', return_value=(kernel, None, None)), \
                    self.assertRaises(ValueError) as error:
                platform._unavailable_process(1)
            self.assertEqual(isinstance(error.exception, platform._ProcessExited), state == 0)

    def test_retired_ancestor_handles_preserve_proven_descendant_lineage(self):
        parent = self.identity(4, 3, 2)
        child = self.identity(5, 4, 3)
        grandchild = self.identity(6, 5, 4)
        snapshots = [[self.root, parent], [self.root, parent, child],
                     [self.root, grandchild], [], []]
        with self.simulation(snapshots) as (ready, entries, releases):
            self.wait(ready)
        self.assertEqual(entries, [3, 4, 5, 6])
        self.assertEqual(releases, [6, 5, 4, 3])
        ready.assert_called_once()

    def test_unknown_root_or_orphan_or_older_child_never_notifies(self):
        for invalid in (self.identity(8, 1, 4), self.identity(8, 999, 4), self.identity(8, 3, 0)):
            with self.subTest(parent=invalid['ppid']), \
                    self.simulation([[self.root], [self.root, invalid]]) as (ready, _, releases):
                with self.assertRaises(ValueError):
                    self.wait(ready)
                ready.assert_not_called()
                self.assertIn(3, releases)

    def test_reused_child_pid_is_rejected_after_notice(self):
        child = self.identity(4, 3, 2)
        reused = self.identity(4, 3, 3)
        with self.simulation([[self.root, child], [self.root, child],
                              [self.root, reused]]) as (ready, _, releases):
            with self.assertRaises(ValueError):
                self.wait(ready)
        ready.assert_called_once()
        self.assertEqual(releases, [4, 3])

    def test_authority_replacement_is_rejected_before_notice(self):
        replacement = self.identity(3, 1, 9)
        with self.simulation([[replacement]]) as (ready, _, _):
            with self.assertRaises(ValueError):
                self.wait(ready)
        ready.assert_not_called()

    def test_main_exit_with_child_alive_does_not_finish_early(self):
        child = self.identity(4, 3, 2)
        with self.simulation([[self.root, child], [self.root, child], [child]]) as (ready, _, releases):
            with self.assertRaises(ValueError):
                self.wait(ready, timeout=2)
        ready.assert_called_once()
        self.assertEqual(releases, [4, 3])

    def test_exit_between_snapshot_and_handle_wait_requires_fresh_absence(self):
        with self.simulation([[self.root], [self.root], [], []]) as (ready, entries, releases):
            kernel = platform._native()[0]
            kernel.WaitForSingleObject.side_effect = None
            kernel.WaitForSingleObject.return_value = 0
            self.wait(ready)
        ready.assert_not_called()
        self.assertEqual(entries, [3])
        self.assertEqual(releases, [3])

    def test_exit_race_rejects_replacement_in_fresh_snapshot(self):
        replacement = self.identity(8, 1, 9)
        for snapshots in ([[self.root], [self.root], [replacement]],
                          [[self.root], [self.root], [], [replacement]]):
            with self.subTest(snapshots=snapshots), self.simulation(snapshots) as result:
                ready, _, releases = result
                kernel = platform._native()[0]
                kernel.WaitForSingleObject.side_effect = None
                kernel.WaitForSingleObject.return_value = 0
                with self.assertRaises(ValueError):
                    self.wait(ready)
            ready.assert_not_called()
            self.assertEqual(releases, [3])

    def test_absent_authority_and_no_process_returns_without_notice(self):
        with self.simulation([[], []]) as (ready, entries, _):
            self.wait(ready)
        ready.assert_not_called()
        self.assertEqual(entries, [])

    def test_callback_failure_releases_all_admitted_descendants(self):
        child = self.identity(4, 3, 2)
        with self.simulation([[self.root, child], [self.root, child]],
                             callback=Mock(side_effect=Exception('private-fixture'))) as result:
            ready, _, releases = result
            with self.assertRaises(ValueError) as error:
                self.wait(ready)
        self.assertNotIn('private-fixture', str(error.exception))
        self.assertEqual(releases, [4, 3])


if __name__ == '__main__':
    unittest.main()
