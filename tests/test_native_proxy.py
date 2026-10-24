"""公式サーバーを使わず、隔離した子プロセスで中継を検査する。"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from native_proxy import NativeProxySocket


class NativeProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.real_popen = subprocess.Popen
        self.thread_errors = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: self.thread_errors.append(args.exc_value)
        self.addCleanup(setattr, threading, 'excepthook', previous_hook)
        self.addCleanup(self.assertEqual, self.thread_errors, [])

    def connect(self, script, timeout=2):
        """Windowsでもshell/shebangを使わずに隔離Python子へ差し替える。"""
        calls = []
        fixture_python = sys.executable
        if os.name == 'nt' and sys.prefix != sys.base_prefix:
            fixture_python = getattr(sys, '_base_executable', None)
            self.assertIsInstance(fixture_python, str)
            self.assertTrue(Path(fixture_python).is_file())
        def fake_child(command, **kwargs):
            calls.append((command, kwargs.copy()))
            return self.real_popen([fixture_python, '-u', '-c', script], **kwargs)
        with patch('native_proxy.subprocess.Popen', side_effect=fake_child):
            raw = NativeProxySocket(sys.executable, timeout=timeout)
        self.addCleanup(raw.close)
        return raw, calls

    def assert_stopped(self, raw):
        self.assertIsNotNone(raw._process.poll())
        self.assertTrue(all(not thread.is_alive() for thread in raw._threads))

    def test_relative_executable_is_rejected_before_spawn(self):
        with patch('native_proxy.subprocess.Popen') as popen:
            with self.assertRaises(ValueError):
                NativeProxySocket('codex')
            popen.assert_not_called()

    def test_command_environment_and_raw_bytes(self):
        script = ('import os\nwhile True:\n data=os.read(0,65536)\n'
                  ' if not data: break\n os.write(1,data)\n')
        raw, calls = self.connect(script)
        payload = b'GET / HTTP/1.1\r\n\x00\xff\x81\x05hello\n'
        raw.sendall(payload)
        self.assertEqual(self.read_exact(raw, len(payload)), payload)
        command, options = calls[0]
        self.assertEqual(command, [sys.executable, 'app-server', 'proxy'])
        self.assertFalse(options['shell'])
        self.assertEqual(options['stdin'], subprocess.PIPE)
        self.assertEqual(options['stdout'], subprocess.PIPE)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['bufsize'], 0)
        self.assertIsNone(options.get('env'))
        raw.close()
        raw.close()
        self.assert_stopped(raw)

    def test_codex_home_overrides_only_environment_key(self):
        seen = []
        def fake_child(command, **kwargs):
            seen.append(kwargs['env'])
            return self.real_popen([sys.executable, '-u', '-c',
                                    'import sys; sys.stdin.buffer.read()'], **kwargs)
        with patch.dict(os.environ, {'PROXY_TEST_SENTINEL': 'keep'}):
            before = os.environ.copy()
            with patch('native_proxy.subprocess.Popen', side_effect=fake_child):
                raw = NativeProxySocket(sys.executable, codex_home=str(self.root))
        self.addCleanup(raw.close)
        expected = dict(before, CODEX_HOME=str(self.root))
        self.assertEqual(seen, [expected])
        raw.close()
        self.assert_stopped(raw)

    def test_windows_proxy_is_launched_without_console(self):
        seen = []
        def fake_child(command, **kwargs):
            seen.append(kwargs.pop('creationflags'))
            return self.real_popen([sys.executable, '-u', '-c',
                                    'import sys; sys.stdin.buffer.read()'], **kwargs)
        fake_os = SimpleNamespace(name='nt', fspath=os.fspath, environ=os.environ,
                                  read=os.read, write=os.write, dup=os.dup, close=os.close)
        with patch('native_proxy.os', fake_os):
            with patch('native_proxy.subprocess.CREATE_NO_WINDOW', 0x08000000, create=True):
                with patch('native_proxy.subprocess.Popen', side_effect=fake_child):
                    raw = NativeProxySocket(sys.executable)
        self.addCleanup(raw.close)
        self.assertEqual(seen, [0x08000000])
        raw.close()
        self.assert_stopped(raw)

    def test_partial_thread_start_failure_reaps_child(self):
        children = []
        def fake_child(command, **kwargs):
            child = self.real_popen([sys.executable, '-u', '-c',
                                     'import time; time.sleep(60)'], **kwargs)
            children.append(child)
            return child
        with patch('native_proxy.subprocess.Popen', side_effect=fake_child):
            with patch('native_proxy.threading.Thread.start', side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError):
                    NativeProxySocket(sys.executable)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdin.closed)
        self.assertTrue(children[0].stdout.closed)

    def test_owned_handle_setup_failures_close_every_allocation(self):
        for failure in ('pipe', 'socket', 'thread', 'start'):
            for fail_at in (1, 2):
                with self.subTest(failure=failure, fail_at=fail_at):
                    children, pipe_fds, relay_sockets, threads = [], [], [], []
                    counts = dict.fromkeys(('pipe', 'socket', 'thread', 'start'), 0)
                    real_dup, real_socket_dup = os.dup, socket.socket.dup
                    real_thread, real_start = threading.Thread, threading.Thread.start
                    def check(stage):
                        counts[stage] += 1
                        if stage == failure and counts[stage] == fail_at:
                            raise RuntimeError('owned handle fixture')
                    def child(command, **kwargs):
                        process = self.real_popen([sys.executable, '-u', '-c',
                                                  'import time; time.sleep(60)'], **kwargs)
                        children.append(process)
                        return process
                    def pipe_dup(fd):
                        check('pipe')
                        result = real_dup(fd)
                        pipe_fds.append(result)
                        self.assertFalse(os.get_inheritable(result))
                        return result
                    def socket_dup(raw):
                        check('socket')
                        result = real_socket_dup(raw)
                        relay_sockets.append(result)
                        self.assertFalse(result.get_inheritable())
                        return result
                    def thread(*args, **kwargs):
                        check('thread')
                        result = real_thread(*args, **kwargs)
                        threads.append(result)
                        return result
                    def start(raw):
                        check('start')
                        return real_start(raw)
                    sockets = socket.socketpair()
                    with patch('native_proxy.socket.socketpair', return_value=sockets), \
                            patch('native_proxy.subprocess.Popen', side_effect=child), \
                            patch('native_proxy.os.dup', side_effect=pipe_dup), \
                            patch.object(socket.socket, 'dup', socket_dup), \
                            patch('native_proxy.threading.Thread', side_effect=thread), \
                            patch.object(real_thread, 'start', start):
                        with self.assertRaisesRegex(RuntimeError, 'owned handle fixture'):
                            NativeProxySocket(sys.executable)
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())
                    self.assertTrue(children[0].stdin.closed and children[0].stdout.closed)
                    self.assertTrue(all(not item.is_alive() for item in threads))
                    self.assertTrue(all(item.fileno() == -1 for item in sockets + tuple(relay_sockets)))
                    for fd in pipe_fds:
                        with self.assertRaises(OSError):
                            os.fstat(fd)

    def test_running_workers_keep_noninheritable_handles_until_exit(self):
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        owned = []
        targets = (NativeProxySocket._to_proxy, NativeProxySocket._from_proxy)
        def worker(index):
            def run(raw, fd, relay):
                owned.append((fd, relay))
                entered[index].set()
                if not release.wait(5):
                    raise AssertionError('fixture worker barrier timed out')
                targets[index](raw, fd, relay)
            return run
        original_join = threading.Thread.join
        def join(thread, *args, **kwargs):
            if thread in raw._threads:
                try:
                    self.assertEqual(raw._relay.fileno(), -1)
                    self.assertTrue(raw._process.stdin.closed and raw._process.stdout.closed)
                    for fd, relay in owned:
                        os.fstat(fd)
                        self.assertFalse(os.get_inheritable(fd))
                        self.assertGreaterEqual(relay.fileno(), 0)
                        self.assertFalse(relay.get_inheritable())
                finally:
                    release.set()
            return original_join(thread, *args, **kwargs)
        with patch.object(NativeProxySocket, '_to_proxy', worker(0)), \
                patch.object(NativeProxySocket, '_from_proxy', worker(1)):
            raw, _ = self.connect('import time; time.sleep(60)')
        self.addCleanup(release.set)
        self.assertTrue(all(event.wait(2) for event in entered))
        # Inspect once: after the first join the first worker has relinquished ownership.
        inspected = False
        def inspect_join(thread, *args, **kwargs):
            nonlocal inspected
            if thread in raw._threads and not inspected:
                inspected = True
                return join(thread, *args, **kwargs)
            return original_join(thread, *args, **kwargs)
        with patch.object(threading.Thread, 'join', inspect_join):
            raw.close()
        self.assertTrue(inspected)
        self.assert_stopped(raw)
        for fd, relay in owned:
            self.assertEqual(relay.fileno(), -1)
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_start_exception_after_worker_claim_does_not_close_live_handles(self):
        entered, release = threading.Event(), threading.Event()
        owned, children, errors = [], [], []
        real_start = threading.Thread.start
        original_target = NativeProxySocket._to_proxy
        def target(raw, fd, relay):
            owned.append((fd, relay))
            entered.set()
            if not release.wait(5):
                raise AssertionError('fixture claimed worker timed out')
            original_target(raw, fd, relay)
        def start(thread):
            real_start(thread)
            self.assertTrue(entered.wait(2))
            raise RuntimeError('post-start fixture')
        def child(command, **kwargs):
            process = self.real_popen([sys.executable, '-u', '-c',
                                      'import time; time.sleep(60)'], **kwargs)
            children.append(process)
            return process
        real_close = NativeProxySocket.close
        def close(raw):
            try:
                fd, relay = owned[0]
                os.fstat(fd)
                self.assertGreaterEqual(relay.fileno(), 0)
            except BaseException as exc:
                errors.append(exc)
            finally:
                release.set()
            return real_close(raw)
        with patch.object(NativeProxySocket, '_to_proxy', target), \
                patch.object(NativeProxySocket, 'close', close), \
                patch.object(threading.Thread, 'start', start), \
                patch('native_proxy.subprocess.Popen', side_effect=child):
            with self.assertRaisesRegex(RuntimeError, 'post-start fixture'):
                NativeProxySocket(sys.executable)
        self.assertEqual(errors, [])
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(owned[0][1].fileno(), -1)
        with self.assertRaises(OSError):
            os.fstat(owned[0][0])

    def test_caller_operations_keep_socket_lease_across_close(self):
        for operation in ('send', 'sendall', 'recv', 'shutdown'):
            with self.subTest(operation=operation):
                entered, release = threading.Event(), threading.Event()
                leases, errors = [], []
                real_operation = getattr(socket.socket, operation)
                raw, _ = self.connect('import time; time.sleep(60)', timeout=0.04)
                def paused(sock, *args):
                    if threading.current_thread() is caller:
                        leases.append((sock, sock.fileno()))
                        entered.set()
                        if not release.wait(5):
                            raise AssertionError('fixture caller barrier timed out')
                    return real_operation(sock, *args)
                def call():
                    argument = (1 if operation == 'recv' else socket.SHUT_WR
                                if operation == 'shutdown' else b'PROXY_BYTES')
                    try:
                        getattr(raw, operation)(argument)
                    except OSError:
                        pass  # shutdown/close may interrupt an in-flight socket call.
                    except BaseException as exc:
                        errors.append(exc)
                caller = threading.Thread(target=call)
                with patch.object(socket.socket, operation, paused):
                    caller.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        lease, fd = leases[0]
                        self.assertFalse(lease.get_inheritable())
                        self.assertEqual(lease.gettimeout(), 0.04)
                        raw.close()
                        self.assertEqual(raw.fileno(), -1)
                        self.assertEqual(lease.fileno(), fd)
                        # Ordinary descriptor allocation must not reuse this live lease.
                        other, peer = socket.socketpair()
                        try:
                            self.assertNotIn(fd, (other.fileno(), peer.fileno()))
                            peer.sendall(b'unrelated')
                            release.set()
                            caller.join(2)
                            self.assertFalse(caller.is_alive())
                            self.assertEqual(other.recv(9), b'unrelated')
                            self.assertEqual(errors, [])
                            self.assertEqual(lease.fileno(), -1)
                        finally:
                            other.close()
                            peer.close()
                    finally:
                        release.set()
                        caller.join(2)
                with self.assertRaises(OSError):
                    getattr(raw, operation)(1 if operation == 'recv' else socket.SHUT_WR
                                            if operation == 'shutdown' else b'x')
                self.assert_stopped(raw)

    def test_caller_dup_failure_preserves_original_socket(self):
        raw, _ = self.connect('import os\nwhile True:\n data=os.read(0,65536)\n'
                              ' if not data: break\n os.write(1,data)\n')
        with patch.object(socket.socket, 'dup', side_effect=OSError('dup fixture')):
            with self.assertRaisesRegex(OSError, 'dup fixture'):
                raw.sendall(b'not sent')
        raw.settimeout(0.5)
        raw.sendall(b'ok')
        self.assertEqual(raw.recv(2), b'ok')
        self.assertEqual(raw.gettimeout(), 0.5)
        raw.close()
        self.assert_stopped(raw)

    def read_exact(self, raw, length):
        chunks = bytearray()
        while len(chunks) < length:
            part = raw.recv(min(65536, length-len(chunks)))
            self.assertTrue(part, 'unexpected EOF')
            chunks.extend(part)
        return bytes(chunks)

    def test_large_stream_has_no_line_or_message_limit(self):
        # Bytes are read concurrently with writes so the bounded socket/pipe
        # buffers do not turn this into a producer/consumer test deadlock.
        script = ('import os\nwhile True:\n data=os.read(0,65536)\n'
                  ' if not data: break\n pending=memoryview(data)\n'
                  ' while pending:\n  count=os.write(1,pending)\n'
                  '  if count <= 0: raise RuntimeError("short write failed")\n'
                  '  pending=pending[count:]\n')
        raw, _ = self.connect(script, timeout=10)
        payload = bytes(range(256)) * 32769
        errors = []
        def send():
            try:
                raw.sendall(payload)
            except Exception as exc:
                errors.append(exc)
        writer = threading.Thread(target=send)
        writer.start()
        try:
            self.assertEqual(self.read_exact(raw, len(payload)), payload)
        finally:
            raw.close()
            writer.join(3)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assert_stopped(raw)

    def test_recv_timeout_and_early_exit_eof(self):
        raw, _ = self.connect('import time; time.sleep(60)', timeout=0.03)
        self.assertEqual(raw.gettimeout(), 0.03)
        with self.assertRaises(socket.timeout):
            raw.recv(1)
        raw.settimeout(0.06)
        self.assertEqual(raw.gettimeout(), 0.06)
        raw.close()
        self.assert_stopped(raw)
        exited, _ = self.connect('pass')
        self.assertEqual(exited.recv(1), b'')
        exited.close()
        self.assert_stopped(exited)

    def test_shutdown_write_delivers_eof_and_response(self):
        raw, _ = self.connect('import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)')
        raw.sendall(b'finish\x00\xff')
        raw.shutdown(socket.SHUT_WR)
        self.assertEqual(self.read_exact(raw, 8), b'finish\x00\xff')
        self.assertEqual(raw.recv(1), b'')
        raw.close()
        self.assert_stopped(raw)

    def test_stdout_eof_preserves_input_until_write_shutdown(self):
        result = self.root / 'input-after-output-eof'
        raw, _ = self.connect('import os,sys,pathlib; os.close(1); '
                              'data=sys.stdin.buffer.read(); '
                              f'pathlib.Path({str(result)!r}).write_bytes(data)')
        self.assertEqual(raw.recv(1), b'')
        raw.sendall(b'input still open')
        raw.shutdown(socket.SHUT_WR)
        raw._process.wait(timeout=2)
        self.assertEqual(result.read_bytes(), b'input still open')
        raw.close()
        self.assert_stopped(raw)

    def test_close_unblocks_full_input_and_full_output_relays(self):
        for script in ('import time; time.sleep(60)',
                       'import os\nwhile True: os.write(1,b"x"*65536)'):
            with self.subTest(script=script):
                raw, _ = self.connect(script, timeout=0.03)
                try:
                    raw.sendall(b'x' * (4 * 1024 * 1024))
                except socket.timeout:
                    pass
                start = time.monotonic()
                raw.close()
                self.assertLess(time.monotonic() - start, 3)
                self.assert_stopped(raw)

    def test_close_releases_owned_io_before_waiting_for_relay_threads(self):
        raw, _ = self.connect('import time; time.sleep(60)')
        observed = []
        original_join = threading.Thread.join
        def join(thread, *args, **kwargs):
            if thread in raw._threads:
                observed.append((raw._process.stdin.closed, raw._process.stdout.closed,
                                 raw._socket.fileno(), raw._relay.fileno()))
            return original_join(thread, *args, **kwargs)
        with patch.object(threading.Thread, 'join', join):
            raw.close()
        self.assertEqual(observed, [(True, True, -1, -1)] * 2)
        self.assert_stopped(raw)

    def assert_close_does_not_reuse_inflight_fd(self, direction):
        """syscall直前で止め、close後の実FD再割当が別ファイルを壊さない。"""
        entered = threading.Event()
        release = threading.Event()
        joining = threading.Event()
        syscall_fds = []
        close_errors = []
        real_read, real_write = os.read, os.write
        original_join = threading.Thread.join
        def syscall(fd, data):
            syscall_fds.append(fd)
            entered.set()
            if not release.wait(5):
                raise AssertionError('fixture syscall barrier timed out')
            return (real_write if direction == 'write' else real_read)(fd, data)
        fake_os = SimpleNamespace(name=os.name, fspath=os.fspath, environ=os.environ,
                                  read=syscall if direction == 'read' else real_read,
                                  write=syscall if direction == 'write' else real_write,
                                  dup=os.dup, close=os.close)
        with patch('native_proxy.os', fake_os):
            raw, _ = self.connect('import time; time.sleep(60)')
            if direction == 'write':
                raw.sendall(b'PROXY_BYTES')
            self.assertTrue(entered.wait(2))
            def join(thread, *args, **kwargs):
                if thread in raw._threads:
                    joining.set()
                    if not release.wait(5):
                        raise AssertionError('fixture join barrier timed out')
                return original_join(thread, *args, **kwargs)
            def close():
                try:
                    raw.close()
                except BaseException as exc:
                    close_errors.append(exc)
            allocated = []
            unrelated = self.root / ('unrelated-' + direction)
            original = b'ORIGINAL_UNRELATED_CONTENT'
            unrelated.write_bytes(original)
            with patch.object(threading.Thread, 'join', join):
                closing = threading.Thread(target=close)
                closing.start()
                try:
                    self.assertTrue(joining.wait(2))
                    # Reuse normally with os.open, never force-close the worker FD.
                    for _ in range(syscall_fds[0] + 1):
                        allocated.append(os.open(unrelated, os.O_RDWR))
                        if allocated[-1] >= syscall_fds[0]:
                            break
                    release.set()
                    original_join(closing, 3)
                    self.assertFalse(closing.is_alive())
                    self.assertEqual(close_errors, [])
                    self.assert_stopped(raw)
                    self.assertEqual(unrelated.read_bytes(), original)
                    self.assertEqual([os.lseek(fd, 0, os.SEEK_CUR)
                                      for fd in allocated], [0] * len(allocated))
                finally:
                    release.set()
                    original_join(closing, 3)
                    for fd in allocated:
                        os.close(fd)

    def test_close_does_not_write_to_reused_unrelated_fd(self):
        self.assert_close_does_not_reuse_inflight_fd('write')

    def test_close_does_not_read_from_reused_unrelated_fd(self):
        self.assert_close_does_not_reuse_inflight_fd('read')

    def test_close_cancels_pending_partial_input_write(self):
        raw = NativeProxySocket.__new__(NativeProxySocket)
        raw._closed = False
        raw._relay = Mock()
        raw._relay.recv.return_value = b'pending'
        raw._process = SimpleNamespace(stdin=Mock())
        def partial_write(_fd, _data):
            raw._closed = True
            return 1
        with patch('native_proxy.os.write', side_effect=partial_write) as write, \
                patch('native_proxy.os.close') as close:
            # A second recv would spin forever in the uncancelled implementation.
            raw._relay.recv.side_effect = [b'pending', b'']
            raw._to_proxy(123, raw._relay)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(raw._relay.recv.call_count, 1)
        close.assert_called_once_with(123)
        raw._relay.close.assert_called_once()

    def test_close_drops_output_returning_from_an_inflight_read(self):
        raw = NativeProxySocket.__new__(NativeProxySocket)
        raw._closed = False
        raw._relay = Mock()
        raw._process = SimpleNamespace(stdout=Mock())
        def read(_fd, _size):
            was_closed = raw._closed
            raw._closed = True
            return b'' if was_closed else b'buffered before close'
        with patch('native_proxy.os.read', side_effect=read) as reads, \
                patch('native_proxy.os.close') as close:
            raw._from_proxy(123, raw._relay)
        raw._relay.sendall.assert_not_called()
        self.assertEqual(reads.call_count, 1)
        close.assert_called_once_with(123)
        raw._relay.close.assert_called_once()

    @unittest.skipIf(os.name == 'nt', 'POSIX signal-only stubborn child fixture')
    def test_close_kills_own_child_after_terminate_timeout(self):
        raw, _ = self.connect('import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.write(1,b"!"); time.sleep(60)')
        self.assertEqual(raw.recv(1), b'!')
        start = time.monotonic()
        raw.close()
        self.assertLess(time.monotonic() - start, 3)
        self.assert_stopped(raw)
        self.assertEqual(raw._process.returncode, -9)

    def test_popen_failure_closes_allocated_sockets(self):
        sockets = socket.socketpair()
        with patch('native_proxy.socket.socketpair', return_value=sockets):
            with patch('native_proxy.subprocess.Popen', side_effect=OSError('fixture')):
                with self.assertRaises(OSError):
                    NativeProxySocket(sys.executable)
        self.assertEqual([part.fileno() for part in sockets], [-1, -1])


if __name__ == '__main__':
    unittest.main()
