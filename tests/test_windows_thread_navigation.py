import ast
import ctypes
import importlib
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import thread_navigation
import windows_thread_navigation as navigation


THREAD = '11111111-1111-4111-8111-111111111111'
URI = 'codex://threads/' + THREAD


class NativeHarness:
    def __init__(self):
        self.handles = [101]
        self.pid = 1234
        self.class_name = 'Chrome_MessageWindow'
        self.ack = 1
        self.result = 1
        self.messages = []
        self.library = SimpleNamespace(
            FindWindowExW=Mock(side_effect=self.find), IsWindow=Mock(return_value=1),
            GetClassNameW=Mock(side_effect=self.name),
            GetWindowThreadProcessId=Mock(side_effect=self.owner),
            SendMessageTimeoutW=Mock(side_effect=self.send))

    def find(self, parent, previous, name, title):
        if parent != -3 or name != 'Chrome_MessageWindow' or title is not None:
            raise AssertionError('Unexpected window search')
        if previous is None:
            return self.handles[0] if self.handles else None
        index = self.handles.index(previous) + 1
        return self.handles[index] if index < len(self.handles) else None

    def name(self, handle, buffer, count):
        buffer.value = self.class_name
        return len(self.class_name)

    def owner(self, handle, pid):
        ctypes.cast(pid, ctypes.POINTER(ctypes.c_uint32)).contents.value = self.pid
        return 12

    def send(self, handle, message, sender, data, flags, timeout, ack):
        value = ctypes.cast(data, ctypes.POINTER(navigation._CopyData)).contents
        raw = ctypes.string_at(value.lpData, value.cbData)
        self.messages.append(dict(handle=handle, message=message, sender=sender,
                                  data=value.dwData, raw=raw, flags=flags, timeout=timeout))
        ctypes.cast(ack, ctypes.POINTER(ctypes.c_size_t)).contents.value = self.ack
        return self.result


class WindowsThreadNavigation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve() / '利用者'
        self.package_root = Path(self.directory.name).resolve() / '公式 アプリ'
        self.executable = self.package_root / 'app/ChatGPT.exe'
        self.identity = dict(pid=1234, ppid=1, start=[1, 2], executable=str(self.executable))
        self.package = dict(root=str(self.package_root), full_name=navigation._PACKAGE,
                            family=navigation.FAMILY)
        self.evidence = dict(platform='win32', home=str(self.home), desktop=self.identity,
                             attestation=dict(package=dict(self.package)))
        self.native = NativeHarness()
        self.start(patch('windows_thread_navigation.sys.platform', 'win32'))
        self.start(patch.dict(os.environ, CODEX_HOME=str(self.home)))
        self.desktop = self.start(patch('windows_thread_navigation.queue_platform.desktop_for_home', return_value=self.evidence))
        self.revalidate = self.start(patch('windows_thread_navigation.queue_platform.revalidate', return_value=True))
        self.verify = self.start(patch('windows_thread_navigation.verify_package', return_value=self.package))
        self.identity_read = self.start(patch('windows_thread_navigation.process_identity', return_value=self.identity))
        self.win_dll = self.start(patch.object(ctypes, 'WinDLL', create=True, return_value=self.native.library))
        self.launch = self.start(patch.object(subprocess, 'Popen', side_effect=AssertionError('No process launch')))
        self.run = self.start(patch.object(subprocess, 'run', side_effect=AssertionError('No direct process launch')))
        self.system = self.start(patch.object(os, 'system', side_effect=AssertionError('No shell')))
        self.startfile = self.start(patch.object(os, 'startfile', create=True, side_effect=AssertionError('No URI handler')))

    def start(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def reject(self, uri=URI, **kwargs):
        with self.assertRaises((ValueError, OSError)):
            navigation.request_open(uri, **kwargs)
        self.native.library.SendMessageTimeoutW.assert_not_called()

    def test_native_copydata_layout_uses_pointer_width_and_dword(self):
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(navigation._CopyData.dwData.offset, 0)
        self.assertEqual(navigation._CopyData.cbData.offset, pointer)
        self.assertEqual(navigation._CopyData.lpData.offset, pointer * 2)
        self.assertEqual(ctypes.sizeof(navigation._CopyData), pointer * 3)
        self.assertEqual(ctypes.sizeof(dict(navigation._CopyData._fields_)['cbData']), 4)

    def test_exact_existing_pid_receives_single_utf16_message(self):
        self.assertTrue(navigation.request_open(URI))
        self.desktop.assert_called_once_with(self.home)
        self.verify.assert_called_once_with(1234, str(self.executable))
        self.native.library.SendMessageTimeoutW.assert_called_once()
        message, = self.native.messages
        expected = 'START\0' + str(self.executable.parent) + '\0' + subprocess.list2cmdline(
            [str(self.executable), '--single-argument', URI]) + '\0'
        self.assertEqual(message['raw'], expected.encode('utf-16-le'))
        self.assertEqual(message['raw'].decode('utf-16-le').split('\0'),
                         ['START', str(self.executable.parent), subprocess.list2cmdline(
                             [str(self.executable), '--single-argument', URI]), ''])
        self.assertEqual((message['handle'], message['message'], message['sender'], message['data']),
                         (101, 0x4a, 0, 0))
        self.assertEqual((message['flags'], message['timeout']), (0x23, 5000))
        self.launch.assert_not_called()
        self.run.assert_not_called()
        self.system.assert_not_called()
        self.startfile.assert_not_called()

    def test_native_signatures_and_result_pointer_are_bound(self):
        navigation._native()
        function = self.native.library.SendMessageTimeoutW
        self.assertEqual(function.argtypes, [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)])
        self.assertIs(function.restype, ctypes.c_ssize_t)
        self.assertEqual(self.native.library.FindWindowExW.argtypes,
                         [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p])

    def test_invalid_uri_never_inspects_or_sends(self):
        for value in (None, '', THREAD, 'https://example.com', URI + '?hostId=elsewhere',
                      URI + '/next', URI + '\n', URI + '\0', URI + ' --command=elsewhere',
                      URI.replace('threads', 'projects')):
            with self.subTest(value=value):
                self.reject(value)
        self.desktop.assert_not_called()
        self.win_dll.assert_not_called()

    def test_common_api_delegates_only_normalized_uri_and_callback(self):
        callback = Mock(return_value=True)
        with patch('windows_thread_navigation.request_open', return_value=True) as request:
            self.assertTrue(thread_navigation.open_thread(THREAD, before_open=callback))
        request.assert_called_once_with(URI, before_open=callback)

    def test_common_api_sanitizes_adapter_failure(self):
        with patch('windows_thread_navigation.request_open', side_effect=ValueError('private path')):
            with self.assertRaises(thread_navigation.ThreadNavigationError) as caught:
                thread_navigation.open_thread(THREAD)
        self.assertNotIn('private path', str(caught.exception))

    def test_unknown_version_architecture_family_and_unattested_package_fail(self):
        for changed in (dict(full_name='OpenAI.Codex_26.9999.1.0_x64__2p2nqsd0c76g0'),
                        dict(full_name=navigation._PACKAGE.replace('x64', 'arm64')),
                        dict(family='another.publisher'), dict(root=str(self.package_root / 'elsewhere'))):
            with self.subTest(changed=changed):
                self.verify.return_value = dict(self.package, **changed)
                self.reject()
        self.win_dll.assert_not_called()
        self.verify.return_value = self.package
        self.evidence['attestation']['package'] = dict(self.package, full_name='unattested')
        self.reject()

    def test_nonofficial_identity_home_and_package_validation_fail(self):
        self.verify.side_effect = ValueError('not signed')
        self.reject()
        self.verify.side_effect = None
        self.evidence['home'] = str(self.home / 'other')
        self.reject()
        self.evidence['home'] = str(self.home)
        self.identity_read.return_value = dict(self.identity, start=[7, 9])
        self.reject()

    def test_zero_multiple_or_wrong_pid_windows_never_send(self):
        for handles in ([], [101, 102]):
            self.native.handles = handles
            self.reject()
        self.native.handles = [101]
        self.native.pid = 9999
        self.reject()

    def test_other_process_windows_do_not_change_unique_official_target(self):
        self.native.handles = [99, 101]
        def owner(handle, pid):
            ctypes.cast(pid, ctypes.POINTER(ctypes.c_uint32)).contents.value = 1234 if handle == 101 else 9999
            return 12
        self.native.library.GetWindowThreadProcessId.side_effect = owner
        self.assertTrue(navigation.request_open(URI))
        self.assertEqual(self.native.messages[0]['handle'], 101)

    def test_dead_wrong_class_and_malformed_handles_never_send(self):
        self.native.library.IsWindow.return_value = 0
        self.reject()
        self.native.library.IsWindow.return_value = 1
        self.native.class_name = 'OtherWindow'
        self.reject()
        self.native.class_name = 'Chrome_MessageWindow'
        for value in (True, -1, '101', 2 ** 100):
            self.native.library.FindWindowExW.return_value = value
            self.native.library.FindWindowExW.side_effect = None
            self.reject()

    def test_identity_change_before_send_never_dispatches(self):
        self.identity_read.side_effect = [self.identity, dict(self.identity, start=[3, 4])]
        self.reject()

    def test_handle_reuse_during_confirmation_never_dispatches(self):
        def changed_target():
            self.native.pid = 4567
            return True
        self.reject(before_open=changed_target)

    def test_confirmation_requires_exact_true(self):
        for value in (False, None, 1, 'yes'):
            self.reject(before_open=lambda: value)

    def test_confirmed_target_is_checked_again_before_single_send(self):
        callback = Mock(return_value=True)
        self.assertTrue(navigation.request_open(URI, before_open=callback))
        callback.assert_called_once_with()
        self.native.library.SendMessageTimeoutW.assert_called_once()

    def test_native_timeout_and_rejected_ack_never_retry(self):
        self.native.result = 0
        with self.assertRaises(ValueError):
            navigation.request_open(URI)
        self.assertEqual(len(self.native.messages), 1)
        self.native.library.SendMessageTimeoutW.reset_mock()
        self.native.ack = 0
        self.native.result = 1
        with self.assertRaises(ValueError):
            navigation.request_open(URI)
        self.native.library.SendMessageTimeoutW.assert_called_once()
        self.assertEqual(len(self.native.messages), 2)

    def test_identity_or_window_change_after_dispatch_reports_failure_without_retry(self):
        old_send = self.native.send
        def changed(*args):
            result = old_send(*args)
            self.native.pid = 9999
            return result
        self.native.library.SendMessageTimeoutW.side_effect = changed
        with self.assertRaises(ValueError):
            navigation.request_open(URI)
        self.native.library.SendMessageTimeoutW.assert_called_once()

    def test_attestation_changes_before_send_never_dispatches(self):
        self.revalidate.side_effect = [True, False]
        self.reject()

    def test_unknown_process_id_and_native_return_types_are_rejected(self):
        self.evidence['desktop'] = dict(self.identity, pid=True)
        self.reject()
        self.evidence['desktop'] = self.identity
        for value in (True, None, '1'):
            self.native.library.IsWindow.return_value = value
            self.reject()

    def test_confirmation_occurs_after_attestation_before_fast_identity_check(self):
        events = []
        self.revalidate.side_effect = lambda proof: events.append('attestation') or True
        self.identity_read.side_effect = lambda pid: events.append('identity') or self.identity
        def confirm():
            events.append('confirmation')
            return True
        navigation.request_open(URI, before_open=confirm)
        position = events.index('confirmation')
        self.assertEqual(events[position + 1], 'identity')
        self.assertEqual(events.count('confirmation'), 1)

    def test_process_reuse_after_message_is_reported_without_retry(self):
        old_send = self.native.send
        def changed(*args):
            result = old_send(*args)
            self.identity_read.return_value = dict(self.identity, start=[7, 8])
            return result
        self.native.library.SendMessageTimeoutW.side_effect = changed
        with self.assertRaises(ValueError):
            navigation.request_open(URI)
        self.native.library.SendMessageTimeoutW.assert_called_once()

    def test_same_handle_loop_is_bounded_and_rejected(self):
        self.native.library.FindWindowExW.side_effect = None
        self.native.library.FindWindowExW.return_value = 101
        self.reject()
        self.assertEqual(self.native.library.FindWindowExW.call_count, 2)

    def test_invalid_ack_and_native_result_types_are_not_success(self):
        self.native.ack = 2
        with self.assertRaises(ValueError):
            navigation.request_open(URI)
        self.native.library.SendMessageTimeoutW.assert_called_once()
        self.native.library.SendMessageTimeoutW.reset_mock()
        self.native.result = True
        self.native.ack = 1
        with self.assertRaises(ValueError):
            navigation.request_open(URI)
        self.native.library.SendMessageTimeoutW.assert_called_once()

    def test_module_import_on_other_platform_does_not_load_native_library(self):
        with patch('windows_thread_navigation.sys.platform', 'linux'):
            importlib.reload(navigation)
            self.reject()
        self.win_dll.assert_not_called()

    def test_new_module_has_no_process_or_shell_launch_call(self):
        tree = ast.parse(Path(navigation.__file__).read_text(encoding='utf-8'))
        forbidden = {'Popen', 'run', 'call', 'check_call', 'check_output', 'system', 'startfile',
                     'ShellExecuteW', 'ShellExecuteExW', 'CreateProcessW', 'WinExec'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden)
