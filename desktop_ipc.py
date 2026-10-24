from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import time
import uuid

from core import is_quota_error, reject_links
from desktop_binding import process_identity


_MAX_FRAME = 16 * 1024 * 1024
_MAX_MESSAGES = 64
_ERROR = '起動中のCodexの実行状態を確認できません。'
_PIPE = r'\\.\pipe\codex-ipc'
_RETAINED_IO = []


class DesktopIpcError(RuntimeError):
    pass


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('identifier')
    return value


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ValueError('timeout')
    return value


class _Overlapped(ctypes.Structure):
    _fields_ = [('internal', ctypes.c_size_t), ('internal_high', ctypes.c_size_t),
                ('offset', wintypes.DWORD), ('offset_high', wintypes.DWORD), ('event', wintypes.HANDLE)]


class _WindowsPipe:
    def __init__(self, deadline):
        self.handle = None
        for retained in list(_RETAINED_IO):
            event, pending, buffer, kernel = retained
            if kernel.WaitForSingleObject(event, 0) == 0:
                kernel.CloseHandle(event)
                _RETAINED_IO.remove(retained)
        if len(_RETAINED_IO) >= 8:
            raise ValueError('pending_io')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = (
            ('CreateFileW', [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            ('WaitNamedPipeW', [wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
            ('CreateEventW', [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            ('ReadFile', [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                          ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            ('WriteFile', [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                           ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            ('GetOverlappedResult', [wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
            ('CancelIoEx', [wintypes.HANDLE, ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            ('GetNamedPipeServerProcessId', [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
            ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL))
        for name, arguments, result in signatures:
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        for _ in range(2):
            handle = self.kernel.CreateFileW(_PIPE, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != ctypes.c_void_p(-1).value and handle is not None:
                self.handle = handle
                return
            if ctypes.get_last_error() != 231 or not self.kernel.WaitNamedPipeW(
                    _PIPE, max(1, int(_remaining(deadline) * 1000))):
                break
        raise ValueError('connect')

    def peer(self):
        pid = wintypes.ULONG()
        if not self.kernel.GetNamedPipeServerProcessId(self.handle, ctypes.byref(pid)):
            raise ValueError('peer')
        return pid.value

    def _io(self, value, writing, deadline):
        _remaining(deadline)
        size = len(value) if writing else value
        buffer = ctypes.create_string_buffer(value, size) if writing else ctypes.create_string_buffer(size)
        event = self.kernel.CreateEventW(None, True, False, None)
        if event is None:
            raise ValueError('event')
        pending = _Overlapped(event=event)
        count = wintypes.DWORD()
        retained = False
        try:
            function = self.kernel.WriteFile if writing else self.kernel.ReadFile
            result = function(self.handle, buffer, size, ctypes.byref(count), ctypes.byref(pending))
            if not result:
                if ctypes.get_last_error() != 997:
                    raise ValueError('io')
                wait = self.kernel.WaitForSingleObject(event, max(0, int((deadline - time.monotonic()) * 1000)))
                if wait != 0:
                    self.kernel.CancelIoEx(self.handle, ctypes.byref(pending))
                    if self.kernel.WaitForSingleObject(event, 1000) != 0:
                        _RETAINED_IO.append((event, pending, buffer, self.kernel))
                        retained = True
                    raise ValueError('timeout')
                if not self.kernel.GetOverlappedResult(self.handle, ctypes.byref(pending), ctypes.byref(count), False):
                    raise ValueError('io')
            if not 0 < count.value <= size:
                raise ValueError('io')
            return count.value if writing else buffer.raw[:count.value]
        finally:
            if not retained:
                self.kernel.CloseHandle(event)

    def read(self, size, deadline):
        return self._io(size, False, deadline)

    def write(self, data, deadline):
        while data:
            data = data[self._io(data, True, deadline):]

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _socket_path(path):
    reject_links(path)
    directory, info = path.parent.lstat(), path.lstat()
    uid = os.getuid()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != uid or directory.st_mode & 0o077
            or not stat.S_ISSOCK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077):
        raise ValueError('socket_permissions')
    return info.st_dev, info.st_ino


class _DarwinSocket:
    def __init__(self, home, deadline):
        path = Path(home) / 'ipc' / 'ipc.sock'
        reject_links(path)
        if not path.exists():
            path = Path(tempfile.gettempdir()) / 'codex-ipc' / ('ipc-' + str(os.getuid()) + '.sock')
        identity = _socket_path(path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.settimeout(_remaining(deadline))
            self.socket.connect(str(path))
            if _socket_path(path) != identity:
                raise ValueError('socket_changed')
        except Exception:
            self.socket.close()
            raise

    def peer(self):
        pid = struct.unpack('i', self.socket.getsockopt(0, 2, struct.calcsize('i')))[0]
        library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        function = library.getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if function(self.socket.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0 or uid.value != os.getuid():
            raise ValueError('peer_uid')
        return pid

    def read(self, size, deadline):
        self.socket.settimeout(_remaining(deadline))
        return self.socket.recv(size)

    def write(self, data, deadline):
        self.socket.settimeout(_remaining(deadline))
        self.socket.sendall(data)

    def close(self):
        self.socket.close()


def _connect(evidence, deadline):
    stream = None
    try:
        desktop = evidence['desktop']
        if process_identity(desktop['pid']) != desktop:
            raise ValueError('desktop_changed')
        if sys.platform == 'win32' and evidence['platform'] == 'win32':
            stream = _WindowsPipe(deadline)
        elif sys.platform == 'darwin' and evidence['platform'] == 'darwin':
            stream = _DarwinSocket(evidence['home'], deadline)
        else:
            raise ValueError('platform')
        if stream.peer() != desktop['pid'] or process_identity(desktop['pid']) != desktop:
            raise ValueError('peer_changed')
        return stream
    except Exception:
        if stream is not None:
            stream.close()
        raise DesktopIpcError(_ERROR) from None


class DesktopIpc:
    def __init__(self, evidence, timeout=5):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 30):
            raise DesktopIpcError(_ERROR)
        self.evidence = evidence
        self.timeout = timeout
        self.stream = None
        self.client_id = 'initializing-client'
        self.following = None
        self.latest = None
        self.messages = 0

    def __enter__(self):
        try:
            self.deadline = time.monotonic() + self.timeout
            self.stream = _connect(self.evidence, self.deadline)
            reply = self._request('initialize', {'clientType': 'desktop'}, version=0)
            self.client_id = _uuid(reply['result']['clientId'])
            if reply.get('handledByClientId') != self.client_id:
                raise ValueError('initialize_identity')
            return self
        except Exception:
            self.close()
            raise DesktopIpcError(_ERROR) from None

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.stream is not None:
            try:
                if self.following is not None:
                    thread, owner = self.following
                    self._following(thread, owner, False)
            except Exception:
                pass
            finally:
                self.stream.close()
                self.stream = None
                self.following = None

    def _send(self, message):
        allowed = ('initialize', 'thread-owner-discovery', 'thread-follower-load-complete-history',
                   'thread-stream-following-changed')
        if message.get('method') not in allowed:
            raise ValueError('method')
        payload = json.dumps(message, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if not 0 < len(payload) <= _MAX_FRAME:
            raise ValueError('frame')
        self.stream.write(struct.pack('<I', len(payload)) + payload, self.deadline)

    def _read_exact(self, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.stream.read(size - len(result), self.deadline)
            if not isinstance(chunk, bytes) or not chunk or len(chunk) > size - len(result):
                raise ValueError('stream')
            result.extend(chunk)
        return result

    def _message(self):
        self.messages += 1
        if self.messages > _MAX_MESSAGES:
            raise ValueError('flood')
        size = struct.unpack('<I', self._read_exact(4))[0]
        if not 0 < size <= _MAX_FRAME:
            raise ValueError('frame')
        def constant(value):
            raise ValueError('constant')
        value = json.loads(self._read_exact(size).decode('utf-8'), parse_constant=constant)
        if not isinstance(value, dict):
            raise ValueError('schema')
        return value

    def _observe(self, message):
        if message.get('type') == 'request':
            raise ValueError('request')
        if message.get('type') == 'broadcast' and message.get('method') == 'thread-stream-state-changed':
            params = message.get('params')
            if not isinstance(params, dict):
                raise ValueError('snapshot')
            if self.following is None or params.get('conversationId') != self.following[0]:
                return
            if (message.get('version') != 11 or message.get('sourceClientId') != self.following[1]
                    or params.get('hostId') != 'local'
                    or (message.get('targetClientIds') is not None
                        and self.client_id not in message['targetClientIds'])):
                raise ValueError('snapshot_origin')
            change = params.get('change')
            if not isinstance(change, dict) or change.get('type') != 'snapshot':
                raise ValueError('snapshot_changed')
            if type(change.get('revision')) is not int or change['revision'] < 0:
                raise ValueError('revision')
            self.latest = change

    def _request(self, method, params, *, target=None, version=1):
        request_id = str(uuid.uuid4())
        message = {'type': 'request', 'requestId': request_id, 'sourceClientId': self.client_id,
                   'method': method, 'params': params, 'version': version,
                   'timeoutMs': max(1, int(_remaining(self.deadline) * 1000))}
        if target is not None:
            message['targetClientId'] = target
        self._send(message)
        while True:
            reply = self._message()
            if reply.get('type') == 'response' and reply.get('requestId') == request_id:
                if (reply.get('resultType') != 'success' or reply.get('method') != method
                        or not isinstance(reply.get('result'), dict)
                        or (target is not None and reply.get('handledByClientId') != target)):
                    raise ValueError('response')
                return reply
            self._observe(reply)

    def _following(self, thread, owner, following):
        self._send({'type': 'broadcast', 'sourceClientId': self.client_id,
                    'method': 'thread-stream-following-changed', 'version': 1,
                    'targetClientIds': [owner], 'params': {'hostId': 'local',
                    'conversationId': thread, 'following': following}})

    def snapshot(self, thread_id):
        try:
            thread = _uuid(thread_id)
            self.deadline = time.monotonic() + self.timeout
            self.messages = 0
            self.latest = None
            owner_reply = self._request('thread-owner-discovery', {'hostId': 'local', 'conversationId': thread})
            owner = _uuid(owner_reply.get('handledByClientId'))
            if self.following is not None and self.following != (thread, owner):
                self._following(*self.following, False)
            self.following = (thread, owner)
            self._following(thread, owner, True)
            reply = self._request('thread-follower-load-complete-history', {'conversationId': thread}, target=owner)
            revision = reply['result'].get('revision')
            if type(revision) is not int or revision < 0:
                raise ValueError('revision')
            while self.latest is None or self.latest['revision'] < revision:
                self._observe(self._message())
            if self.latest['revision'] != revision:
                raise ValueError('snapshot_race')
            return _snapshot(self.latest.get('conversationState'), thread)
        except Exception:
            raise DesktopIpcError(_ERROR) from None


def _snapshot(state, thread):
    if not isinstance(state, dict) or state.get('id') != thread or state.get('hostId', 'local') != 'local':
        raise ValueError('thread')
    if state.get('resumeState') != 'resumed':
        raise ValueError('resume_state')
    runtime = state.get('threadRuntimeStatus')
    if not isinstance(runtime, dict) or runtime.get('type') not in ('idle', 'active'):
        raise ValueError('runtime')
    flags = runtime.get('activeFlags', [])
    if not isinstance(flags, list) or any(flag not in ('waitingOnApproval', 'waitingOnUserInput') for flag in flags):
        raise ValueError('flags')
    requests = state.get('requests')
    if not isinstance(requests, list) or any(not isinstance(request, dict) for request in requests):
        raise ValueError('requests')
    turns = state.get('turns')
    if not isinstance(turns, list):
        raise ValueError('turns')
    history = state.get('turnHistory')
    if history is not None:
        if not isinstance(history, dict) or history.get('kind') != 'canonical':
            raise ValueError('history')
        value = history.get('history')
        if (not isinstance(value, dict) or value.get('isComplete') is not True
                or not isinstance(value.get('islands'), list) or len(value['islands']) != 1):
            raise ValueError('history')
        entries = value['islands'][0].get('entries')
        entities = value.get('entitiesByKey')
        if not isinstance(entries, list) or not entries or not isinstance(entities, dict):
            raise ValueError('history')
        key = entries[-1].get('value')
        turns = [entities.get(key)]
    if not turns or not isinstance(turns[-1], dict):
        raise ValueError('turn')
    turn = turns[-1]
    turn_id = _uuid(turn.get('turnId'))
    status = turn.get('status')
    if status not in ('failed', 'completed', 'interrupted', 'inProgress'):
        raise ValueError('turn_status')
    awaiting = bool(flags or requests)
    for field in ('externalRequests', 'unconfirmedTurnSubmissions'):
        pending = state.get(field, [])
        if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
            raise ValueError('pending')
        awaiting = awaiting or bool(pending)
    limited = status == 'failed' and is_quota_error(turn.get('error'))
    return {'id': thread, 'latest_turn': turn_id, 'turn_status': status, 'limited': limited,
            'failure_turn': turn_id if limited else None, 'awaiting': awaiting,
            'idle': runtime['type'] == 'idle' and status != 'inProgress', 'observation_verified': True}
