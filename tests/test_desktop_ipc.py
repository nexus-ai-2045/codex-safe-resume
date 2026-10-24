import json
from pathlib import Path
import struct
import stat
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import desktop_ipc

THREAD = '01a161ab-e82d-7631-a8aa-d33c77b2128e'
TURN = '01a161ab-e82d-7631-a8aa-d33c77b21281'
OWNER = '01a161ab-e82d-7631-a8aa-d33c77b21282'
CLIENT = '01a161ab-e82d-7631-a8aa-d33c77b21283'


def state(**changes):
    result = {'id': THREAD, 'hostId': 'local', 'resumeState': 'resumed',
              'threadRuntimeStatus': {'type': 'idle'}, 'requests': [],
              'turns': [{'turnId': TURN, 'status': 'completed', 'error': None}]}
    result.update(changes)
    return result


class Stream:
    def __init__(self, snapshot=None, source=OWNER, revision=6, version=11):
        self.snapshot = state() if snapshot is None else snapshot
        self.source = source
        self.revision = revision
        self.version = version
        self.data = bytearray()
        self.sent = []
        self.closed = False

    def push(self, value):
        payload = json.dumps(value).encode()
        self.data.extend(struct.pack('<I', len(payload)) + payload)

    def write(self, payload, deadline):
        value = json.loads(payload[4:])
        self.sent.append(value)
        if value['type'] == 'broadcast':
            return
        method = value['method']
        result = {'clientId': CLIENT} if method == 'initialize' else {}
        if method == 'thread-follower-load-complete-history':
            self.push({'type': 'broadcast', 'method': 'thread-stream-state-changed',
                       'sourceClientId': self.source, 'version': self.version,
                       'targetClientIds': [CLIENT], 'params': {'conversationId': THREAD, 'hostId': 'local',
                       'change': {'type': 'snapshot', 'revision': self.revision,
                                  'conversationState': self.snapshot}}})
            result = {'revision': 6}
        self.push({'type': 'response', 'requestId': value['requestId'], 'method': method,
                   'resultType': 'success', 'handledByClientId': CLIENT if method == 'initialize' else OWNER,
                   'result': result})

    def read(self, size, deadline):
        count = min(size, 7, len(self.data))
        value = bytes(self.data[:count])
        del self.data[:count]
        return value

    def close(self):
        self.closed = True


class DesktopIpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = {'desktop': {'pid': 123, 'start': [1, 2], 'executable': 'fixture'},
                         'home': self.temp.name, 'platform': 'win32'}

    def snapshot(self, stream=None):
        stream = stream or Stream()
        with patch('desktop_ipc._connect', return_value=stream):
            with desktop_ipc.DesktopIpc(self.evidence) as client:
                result = client.snapshot(THREAD)
        return result, stream

    def test_live_complete_state_and_readonly_subscription_contract(self):
        value, stream = self.snapshot()
        self.assertEqual(value['latest_turn'], TURN)
        self.assertEqual(value['turn_status'], 'completed')
        self.assertTrue(value['observation_verified'])
        self.assertTrue(value['idle'])
        self.assertFalse(value['limited'])
        self.assertTrue(stream.closed)
        self.assertEqual([x['method'] for x in stream.sent], [
            'initialize', 'thread-owner-discovery', 'thread-stream-following-changed',
            'thread-follower-load-complete-history', 'thread-stream-following-changed'])
        self.assertEqual(stream.sent[0]['sourceClientId'], 'initializing-client')
        self.assertEqual(stream.sent[2]['params'], {'hostId': 'local', 'conversationId': THREAD, 'following': True})
        self.assertEqual(stream.sent[-1]['params']['following'], False)

    def test_structured_quota_failure_and_waiting_flags(self):
        snapshot = state(turns=[{'turnId': TURN, 'status': 'failed',
                                'error': {'codexErrorInfo': 'usageLimitExceeded'}}],
                         threadRuntimeStatus={'type': 'active', 'activeFlags': ['waitingOnApproval']})
        value, _ = self.snapshot(Stream(snapshot))
        self.assertTrue(value['limited'])
        self.assertEqual(value['failure_turn'], TURN)
        self.assertTrue(value['awaiting'])
        self.assertFalse(value['idle'])

    def test_owner_revision_version_and_thread_mismatch_fail_closed(self):
        for stream in (Stream(source=CLIENT), Stream(revision=5), Stream(version=12),
                       Stream(state(id=CLIENT)), Stream(state(hostId='durable'))):
            with self.subTest(stream=stream), self.assertRaises(desktop_ipc.DesktopIpcError):
                self.snapshot(stream)
            self.assertTrue(stream.closed)

    def test_unknown_runtime_flags_or_incomplete_status_are_rejected(self):
        for snapshot in (state(threadRuntimeStatus=None),
                         state(threadRuntimeStatus={'type': 'active', 'activeFlags': ['futureFlag']}),
                         state(turns=[{'turnId': TURN, 'status': 'unknown'}]),
                         state(requests='invalid')):
            with self.assertRaises(desktop_ipc.DesktopIpcError):
                self.snapshot(Stream(snapshot))

    def test_native_peer_identity_is_checked_before_and_after_connection(self):
        desktop = self.evidence['desktop']
        stream = Stream()
        stream.peer = lambda: desktop['pid']
        with patch('desktop_ipc.process_identity', return_value=desktop), \
                patch('desktop_ipc.sys.platform', 'win32'), \
                patch('desktop_ipc._WindowsPipe', return_value=stream):
            self.assertIs(desktop_ipc._connect(self.evidence, 999999999999), stream)
        for identities, peer in (([desktop, dict(desktop, start=[3, 4])], desktop['pid']),
                                 ([desktop], desktop['pid'] + 1)):
            stream = Stream()
            stream.peer = lambda: peer
            with patch('desktop_ipc.process_identity', side_effect=identities), \
                    patch('desktop_ipc.sys.platform', 'win32'), \
                    patch('desktop_ipc._WindowsPipe', return_value=stream):
                with self.assertRaises(desktop_ipc.DesktopIpcError):
                    desktop_ipc._connect(self.evidence, 999999999999)
            self.assertTrue(stream.closed)

    def test_needs_resume_and_unknown_pending_shapes_fail_closed(self):
        for snapshot in (state(resumeState='needs_resume'), state(externalRequests=None),
                         state(unconfirmedTurnSubmissions=['invalid'])):
            with self.assertRaises(desktop_ipc.DesktopIpcError):
                self.snapshot(Stream(snapshot))

    def test_non_target_broadcast_flood_is_bounded(self):
        stream = Stream()
        stream.write = lambda payload, deadline: None
        for _ in range(65):
            stream.push({'type': 'broadcast', 'method': 'unrelated', 'params': {}})
        with self.assertRaises(desktop_ipc.DesktopIpcError):
            self.snapshot(stream)
        self.assertTrue(stream.closed)

    def test_pending_requests_never_report_clear(self):
        value, _ = self.snapshot(Stream(state(requests=[{'id': 'approval'}])))
        self.assertTrue(value['awaiting'])

    def test_frame_size_checked_before_body(self):
        stream = Stream()
        stream.write = lambda payload, deadline: None
        stream.data = bytearray(struct.pack('<I', 16 * 1024 * 1024 + 1))
        with self.assertRaises(desktop_ipc.DesktopIpcError):
            self.snapshot(stream)
        self.assertEqual(len(stream.data), 0)

    def test_canonical_history_uses_last_island_entry_not_id_sort(self):
        history = {'isComplete': True, 'islands': [{'entries': [{'value': 'new'}, {'value': 'old'}]}],
                   'entitiesByKey': {'new': {'turnId': CLIENT, 'status': 'failed'},
                                     'old': {'turnId': TURN, 'status': 'completed'}}}
        value, _ = self.snapshot(Stream(state(turnHistory={'kind': 'canonical', 'history': history})))
        self.assertEqual(value['latest_turn'], TURN)

    def test_socket_endpoint_requires_current_uid_private_modes_and_socket(self):
        path = Path('/fixture/ipc/ipc.sock')
        directory = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=123)
        endpoint = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=123, st_dev=1, st_ino=2)
        with patch('desktop_ipc.reject_links'), patch('desktop_ipc.os.getuid', return_value=123, create=True), \
                patch('desktop_ipc.Path.lstat', side_effect=[directory, endpoint]):
            self.assertEqual(desktop_ipc._socket_path(path), (1, 2))
        for parent_mode, leaf_mode, owner in ((stat.S_IFDIR | 0o755, stat.S_IFSOCK | 0o600, 123),
                                             (stat.S_IFDIR | 0o700, stat.S_IFREG | 0o600, 123),
                                             (stat.S_IFDIR | 0o700, stat.S_IFSOCK | 0o666, 123),
                                             (stat.S_IFDIR | 0o700, stat.S_IFSOCK | 0o600, 124)):
            directory = SimpleNamespace(st_mode=parent_mode, st_uid=owner)
            endpoint = SimpleNamespace(st_mode=leaf_mode, st_uid=owner, st_dev=1, st_ino=2)
            with patch('desktop_ipc.reject_links'), patch('desktop_ipc.os.getuid', return_value=123, create=True), \
                    patch('desktop_ipc.Path.lstat', side_effect=[directory, endpoint]):
                with self.assertRaises(ValueError):
                    desktop_ipc._socket_path(path)

    def test_windows_timeout_cancels_only_own_io_and_keeps_unfinished_buffers_alive(self):
        for waits, retained in (([258, 0], False), ([258, 258], True)):
            pipe = desktop_ipc._WindowsPipe.__new__(desktop_ipc._WindowsPipe)
            pipe.handle = 123
            pipe.kernel = Mock()
            pipe.kernel.CreateEventW.return_value = 321
            pipe.kernel.ReadFile.return_value = False
            pipe.kernel.WaitForSingleObject.side_effect = waits
            previous = list(desktop_ipc._RETAINED_IO)
            try:
                with patch('desktop_ipc.ctypes.get_last_error', return_value=997, create=True):
                    with self.assertRaises(ValueError):
                        pipe.read(4, time.monotonic() + .01)
                pipe.kernel.CancelIoEx.assert_called_once()
                self.assertEqual(pipe.kernel.CancelIoEx.call_args.args[0], 123)
                self.assertEqual(len(desktop_ipc._RETAINED_IO), len(previous) + int(retained))
                if retained:
                    pipe.kernel.CloseHandle.assert_not_called()
                else:
                    pipe.kernel.CloseHandle.assert_called_once_with(321)
            finally:
                desktop_ipc._RETAINED_IO[:] = previous

    def test_unknown_or_disconnected_peer_never_initializes(self):
        with patch('desktop_ipc._connect', side_effect=ValueError('private-path')):
            with self.assertRaises(desktop_ipc.DesktopIpcError) as caught:
                with desktop_ipc.DesktopIpc(self.evidence):
                    pass
        self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
