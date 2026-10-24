"""チャットを開く要求は送信・状態DB・任意コマンド実行から分離する。"""
import plistlib
import ctypes
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import thread_navigation as navigation


THREAD = '11111111-1111-4111-8111-111111111111'


class ThreadNavigation(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bundle = Path(directory.name).resolve() / 'Codex.app'
        executable = self.bundle / 'Contents/MacOS/Codex'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'not executed')
        info = {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Codex',
                'CFBundleURLTypes': [{'CFBundleURLSchemes': ['codex']}]}
        (self.bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        self.identity = dict(pid=1234, ppid=1, start=[1, 2], executable=str(executable))
        self.running = Mock()
        self.running.processIdentifier.return_value = 1234
        self.running.isTerminated.return_value = False
        self.running.activateWithOptions_.return_value = True
        self.apps = Mock(return_value=[self.running])
        self.start_patch(patch.dict(sys.modules, AppKit=SimpleNamespace(
            NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=self.apps,
                runningApplicationWithProcessIdentifier_=Mock(return_value=self.running)),
            NSApplicationActivateIgnoringOtherApps=2)))
        self.start_patch(patch('thread_navigation.sys.platform', 'darwin'))
        self.process = self.start_patch(patch('thread_navigation.process_identity', return_value=self.identity))
        self.start_patch(patch('thread_navigation.desktop_bundle', return_value='com.openai.codex'))
        self.start_patch(patch('thread_navigation.validate_location', side_effect=lambda value: value))
        self.run = self.start_patch(patch('thread_navigation.subprocess.run', return_value=SimpleNamespace(returncode=0)))
        self.real_permission = navigation._event_permission
        self.permission = self.start_patch(patch('thread_navigation._event_permission', return_value=True))
        self.event = Mock()
        self.event.sendEventWithOptions_timeout_error_.return_value = (None, None)
        self.descriptors = SimpleNamespace(
            descriptorWithProcessIdentifier_=Mock(return_value='pid-address'),
            descriptorWithString_=Mock(side_effect=lambda text: text),
            appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_=Mock(return_value=self.event))
        self.start_patch(patch.dict(sys.modules, Foundation=SimpleNamespace(
            NSAppleEventDescriptor=self.descriptors,
            NSAppleEventSendNoReply=1, NSAppleEventSendNeverInteract=16)))

    def start_patch(self, value):
        result = value.start()
        self.addCleanup(value.stop)
        return result

    def test_requests_exact_uri_only_in_verified_running_app(self):
        navigation.open_thread(THREAD)
        signature, = self.run.call_args_list
        self.assertEqual(signature.args[0][0], '/usr/bin/codesign')
        self.assertIn('--deep', signature.args[0])
        self.descriptors.descriptorWithProcessIdentifier_.assert_called_once_with(1234)
        self.event.setParamDescriptor_forKeyword_.assert_called_once_with(
            'codex://threads/' + THREAD, int.from_bytes(b'----', 'big'))
        self.event.sendEventWithOptions_timeout_error_.assert_called_once_with(0x00020011, 5, None)
        self.running.activateWithOptions_.assert_called_once_with(2)

    def test_address_matches_sdk_two_byte_packing(self):
        self.assertEqual(navigation._Address.data_handle.offset, 4)
        self.assertEqual(ctypes.sizeof(navigation._Address), 4 + ctypes.sizeof(ctypes.c_void_p))

    def test_invalid_identifiers_never_request_open(self):
        for value in ('', None, '../elsewhere', THREAD + '?hostId=elsewhere', THREAD + '\n'):
            with self.subTest(value=value), self.assertRaises(navigation.ThreadNavigationError):
                navigation.open_thread(value)
        self.run.assert_not_called()
        self.apps.assert_not_called()

    def test_missing_multiple_or_terminated_apps_never_launch(self):
        for apps in ([], [self.running, self.running]):
            self.apps.return_value = apps
            with self.assertRaises(navigation.ThreadNavigationError):
                navigation.open_thread(THREAD)
        self.apps.return_value = [self.running]
        self.running.isTerminated.return_value = True
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD)
        self.run.assert_not_called()

    def test_failed_signature_does_not_open(self):
        self.run.return_value.returncode = 1
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD)
        self.assertEqual(self.run.call_count, 1)

    def test_changed_identity_does_not_open(self):
        self.process.side_effect = [self.identity, dict(self.identity, start=[3, 4])]
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD)
        self.assertEqual(self.run.call_count, 1)

    def test_missing_uri_support_does_not_open(self):
        path = self.bundle / 'Contents/Info.plist'
        info = plistlib.loads(path.read_bytes())
        info['CFBundleURLTypes'] = []
        path.write_bytes(plistlib.dumps(info))
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD)
        self.run.assert_not_called()

    def test_failures_do_not_disclose_paths_or_native_errors(self):
        self.run.side_effect = OSError(str(self.bundle))
        with self.assertRaises(navigation.ThreadNavigationError) as caught:
            navigation.open_thread(THREAD)
        self.assertNotIn(str(self.bundle), str(caught.exception))

    def test_unverified_platform_has_no_fallback(self):
        for platform in ('win32', 'linux'):
            with patch('thread_navigation.sys.platform', platform):
                with self.assertRaises(navigation.ThreadNavigationError):
                    navigation.open_thread(THREAD)
        self.run.assert_not_called()
        self.apps.assert_not_called()

    def test_cancelled_request_does_not_dispatch_after_verification(self):
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD, before_open=lambda: False)
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0][0], '/usr/bin/codesign')

    def test_confirmation_must_be_exact_true(self):
        for value in (None, 1, 'yes'):
            with self.subTest(value=value), self.assertRaises(navigation.ThreadNavigationError):
                navigation.open_thread(THREAD, before_open=lambda: value)
        self.assertTrue(all(item.args[0][0] == '/usr/bin/codesign'
                            for item in self.run.call_args_list))

    def test_missing_event_permission_does_not_prompt_or_send(self):
        self.permission.return_value = False
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD)
        self.event.sendEventWithOptions_timeout_error_.assert_not_called()
        self.assertEqual(self.run.call_count, 1)

    def test_exited_target_cannot_trigger_application_launch(self):
        self.event.sendEventWithOptions_timeout_error_.return_value = (None, Mock())
        with self.assertRaises(navigation.ThreadNavigationError):
            navigation.open_thread(THREAD)
        self.descriptors.descriptorWithProcessIdentifier_.assert_called_once_with(1234)
        self.assertEqual(self.run.call_count, 1)
        self.running.activateWithOptions_.assert_not_called()

    def test_permission_probe_never_prompts_and_releases_address(self):
        library = Mock()
        library.AECreateDesc.return_value = 0
        for result in (0, -1743, -1744, -600):
            library.reset_mock()
            library.AEDeterminePermissionToAutomateTarget.return_value = result
            with patch('thread_navigation.ctypes.CDLL', return_value=library):
                self.assertEqual(self.real_permission(1234), result == 0)
            self.assertIs(library.AEDeterminePermissionToAutomateTarget.call_args.args[-1], False)
            library.AEDisposeDesc.assert_called_once()

    def test_failed_address_creation_never_probes_or_sends(self):
        library = Mock()
        library.AECreateDesc.return_value = -1
        with patch('thread_navigation.ctypes.CDLL', return_value=library):
            self.assertFalse(self.real_permission(1234))
        library.AEDeterminePermissionToAutomateTarget.assert_not_called()
        library.AEDisposeDesc.assert_not_called()
