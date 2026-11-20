"""既存サーバーや実データに触れず、stdioとWebSocketの境界を検査する。"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import textwrap
import sys
import threading
import time
import unittest
from unittest.mock import patch

import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_stdio import DesktopStdioError, MAX_FRAME_BYTES, relay


class ShortWriter(io.BytesIO):
    def write(self, value):
        return super().write(value[:3])


class DesktopStdioTests(unittest.TestCase):
    def test_proxy_failure_does_not_consume_input_and_allows_safe_fallback(self):
        from desktop_stdio import RelayPreparationError
        source = io.BytesIO(b'{"id":1,"method":"initialize"}\n')
        with patch('desktop_stdio.NativeProxySocket', side_effect=OSError('unavailable')):
            with self.assertRaises(RelayPreparationError) as caught:
                relay('/fixture/codex', stdin=source, stdout=io.BytesIO())
        self.assertEqual(source.tell(), 0)
        self.assertEqual(caught.exception.reason, 'relay_preparation')

    def setUp(self):
        self.errors = []
        self.threads = []
        self.sockets = []
        self.streams = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for stream in self.streams:
            stream.close()
        for thread in self.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        # relay closes the client first; let the peer observe EOF before closing
        # its descriptor. Closing it earlier races the fixture's final recv.
        for sock in self.sockets:
            sock.close()
        self.assertEqual(self.errors, [])

    def input_pipe(self, payload=b''):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, 'rb', buffering=0)
        writer = os.fdopen(write_fd, 'wb', buffering=0)
        self.streams.extend([reader, writer])
        if payload:
            writer.write(payload)
        return reader, writer

    def proxy(self, action, *, skip_utf8_validation=False):
        client, peer = socket.socketpair()
        self.sockets.extend([client, peer])
        peer.settimeout(5)

        def serve():
            try:
                header = bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    chunk = peer.recv(1)
                    if not chunk:
                        return
                    header.extend(chunk)
                key = next(line.split(b':', 1)[1].strip() for line in
                           header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
                accept = base64.b64encode(hashlib.sha1(
                    key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                peer.sendall(b'HTTP/1.1 101 Switching Protocols\r\n'
                             b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                             b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                server = websocket.WebSocket(skip_utf8_validation=skip_utf8_validation)
                server.sock = peer
                server.connected = True
                action(server, peer)
            except Exception as exc:
                self.errors.append(exc)
            finally:
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        thread = threading.Thread(target=serve)
        self.threads.append(thread)
        thread.start()
        return client

    @staticmethod
    def send(server, text, opcode=websocket.ABNF.OPCODE_TEXT, fin=1):
        frame = websocket.ABNF(fin=fin, opcode=opcode, mask_value=0,
                               data=text.encode('utf-8') if isinstance(text, str) else text)
        server.send_frame(frame)

    def run_relay(self, raw, stdin, stdout=None, **kwargs):
        output = stdout if stdout is not None else io.BytesIO()
        with patch('desktop_stdio.NativeProxySocket', return_value=raw) as native:
            relay('/trusted/codex', codex_home='/isolated/home', stdin=stdin,
                  stdout=output, **kwargs)
        native.assert_called_once_with('/trusted/codex', timeout=kwargs.get('timeout', 8),
                                       codex_home='/isolated/home')
        self.assertEqual(raw.fileno(), -1)
        return output.getvalue()

    def test_full_duplex_preserves_rpc_and_has_no_generated_initialize_or_approval(self):
        sent = [
            {'id': 10, 'method': 'initialize', 'params': {'clientInfo': {'name': 'Desktop'}}},
            {'method': 'initialized', 'params': {}},
            {'id': 'approval', 'result': {'decision': 'decline'}},
            {'id': 'auth', 'method': 'account/login/start', 'params': {'type': 'chatgpt'}},
        ]
        received = [
            {'id': 10, 'result': {'userAgent': 'fixture'}},
            {'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}},
            {'id': 90, 'method': 'item/tool/requestUserInput', 'params': {'questions': []}},
            {'method': 'thread/status/changed', 'params': {'status': 'idle'}},
        ]
        observed = []
        def action(server, peer):
            for expected, response in zip(sent, received):
                observed.append(json.loads(server.recv()))
                self.send(server, json.dumps(response, indent=2))
            peer.settimeout(0.1)
            with self.assertRaises(websocket.WebSocketTimeoutException):
                server.recv()
        reader, _ = self.input_pipe(b''.join(json.dumps(item).encode() + b'\n' for item in sent))
        raw = self.proxy(action)
        output = self.run_relay(raw, reader, ShortWriter())
        self.assertEqual(observed, sent)
        self.assertEqual([json.loads(line) for line in output.splitlines()], received)

    def test_network_eof_does_not_leave_stdin_thread(self):
        reader, _ = self.input_pipe()
        raw = self.proxy(lambda server, peer: None)
        before = {thread.ident for thread in threading.enumerate()}
        start = time.monotonic()
        self.run_relay(raw, reader)
        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse([thread for thread in threading.enumerate()
                          if thread.ident not in before and thread.name == 'desktop-stdio-input'])

    def test_idle_outlives_handshake_timeout(self):
        reader, _ = self.input_pipe()
        def action(server, peer):
            time.sleep(0.12)
            self.send(server, '{"method":"ready"}')
        raw = self.proxy(action)
        self.assertEqual(json.loads(self.run_relay(raw, reader, timeout=0.03)), {'method': 'ready'})

    def test_transient_receive_timeout_does_not_disconnect(self):
        from desktop_stdio import _BoundedWebSocket

        reader, _ = self.input_pipe()
        raw = self.proxy(lambda server, peer: self.send(server, '{"method":"ready"}'))
        original = _BoundedWebSocket.recv_data
        calls = []
        def temporary_timeout(ws, *args, **kwargs):
            calls.append(ws.gettimeout())
            if len(calls) == 1:
                raise websocket.WebSocketTimeoutException('fixture idle')
            return original(ws, *args, **kwargs)
        with patch.object(_BoundedWebSocket, 'recv_data', temporary_timeout):
            output = self.run_relay(raw, reader)
        self.assertEqual(json.loads(output), {'method': 'ready'})
        self.assertTrue(all(timeout is None for timeout in calls))

    def test_handshake_has_a_total_deadline_even_with_dripping_bytes(self):
        raw, peer = socket.socketpair()
        self.sockets.extend([raw, peer])
        def drip():
            try:
                for byte in b'HTTP/1.1 101 Switching Protocols':
                    peer.sendall(bytes([byte]))
                    time.sleep(0.02)
            except OSError:
                pass
        thread = threading.Thread(target=drip)
        self.threads.append(thread)
        thread.start()
        start = time.monotonic()
        with patch('desktop_stdio.NativeProxySocket', return_value=raw):
            with self.assertRaises(DesktopStdioError):
                relay('/trusted/codex', stdin=io.BytesIO(), stdout=io.BytesIO(), timeout=0.08)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(raw.fileno(), -1)

    def test_stdin_eof_terminates_owned_connection(self):
        eof = threading.Event()
        def action(server, peer):
            if not peer.recv(1):
                eof.set()
        raw = self.proxy(action)
        self.run_relay(raw, io.BytesIO())
        self.assertTrue(eof.wait(1))

    def test_stdin_eof_exits_even_when_stdout_pipe_is_full(self):
        # A child contains the regression: a broken writer must not hang this
        # test process, and stdout stays unread until the child has exited.
        script = textwrap.dedent("""
            import pathlib, sys, threading
            from unittest.mock import patch
            import desktop_stdio as stdio
            marker = pathlib.Path(sys.argv[1])
            class Raw:
                def shutdown(self, how): pass
                def close(self): marker.with_suffix('.closed').touch()
            class WS:
                def connect(self, *args, **kwargs): pass
                def settimeout(self, value): pass
                def abort(self): pass
                def recv_data(self):
                    return 1, b'{"value":"' + b'x' * 262144 + b'"}'
            original = stdio._write_all
            def observed(*args, **kwargs):
                marker.touch()
                return original(*args, **kwargs)
            with patch.object(stdio, 'NativeProxySocket', return_value=Raw()), \
                 patch.object(stdio, '_BoundedWebSocket', return_value=WS()), \
                 patch.object(stdio, '_write_all', side_effect=observed):
                stdio.relay('/fixture/codex')
            assert not any(t.name == 'desktop-stdio-input' for t in threading.enumerate())
        """)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'writing'
            child = subprocess.Popen(
                [sys.executable, '-u', '-c', script, str(marker)],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), 'child did not reach stdout writing')
                time.sleep(0.1)
                self.assertIsNone(child.poll())
                child.stdin.close()
                child.wait(timeout=2)
                self.assertEqual(child.returncode, 0, child.stderr.read().decode())
                self.assertTrue(marker.with_suffix('.closed').exists())
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()

    def test_invalid_input_is_rejected_without_forwarding_body(self):
        samples = [b'[]\n', b'null\n', b'broken-private-body\n', b'{"x":1}',
                   b'{"x":NaN}\n', b'\xff\n', b'\n', b'x' * (MAX_FRAME_BYTES + 1)]
        for data in samples:
            with self.subTest(sample=data[:20]):
                observed = []
                def action(server, peer):
                    try:
                        observed.append(server.recv())
                    except websocket.WebSocketConnectionClosedException:
                        pass
                raw = self.proxy(action)
                with patch('desktop_stdio.NativeProxySocket', return_value=raw):
                    with self.assertRaises(DesktopStdioError) as caught:
                        relay('/trusted/codex', stdin=io.BytesIO(data), stdout=io.BytesIO())
                self.assertNotIn('private-body', str(caught.exception))
                self.assertEqual(caught.exception.reason, 'stdin_frame')
                self.assertEqual(raw.fileno(), -1)
                self.assertEqual(observed, [])

    def test_invalid_server_frames_fail_closed(self):
        samples = [('[]', websocket.ABNF.OPCODE_TEXT),
                   ('secret-broken', websocket.ABNF.OPCODE_TEXT),
                   ('{"x":NaN}', websocket.ABNF.OPCODE_TEXT),
                   (b'{}', websocket.ABNF.OPCODE_BINARY)]
        for data, opcode in samples:
            with self.subTest(data=data):
                reader, _ = self.input_pipe()
                raw = self.proxy(lambda server, peer: self.send(server, data, opcode))
                output = io.BytesIO()
                with patch('desktop_stdio.NativeProxySocket', return_value=raw):
                    with self.assertRaises(DesktopStdioError) as caught:
                        relay('/trusted/codex', stdin=reader, stdout=output)
                self.assertNotIn('secret-broken', str(caught.exception))
                self.assertEqual(caught.exception.reason, 'websocket_frame')
                self.assertEqual(output.getvalue(), b'')

    def test_oversize_server_header_is_rejected_before_reading_payload(self):
        reader, _ = self.input_pipe()
        def action(server, peer):
            peer.sendall(b'\x81\x7f' + struct.pack('!Q', MAX_FRAME_BYTES + 1))
            self.assertEqual(peer.recv(1), b'')
        raw = self.proxy(action)
        start = time.monotonic()
        with patch('desktop_stdio.NativeProxySocket', return_value=raw):
            with self.assertRaises(DesktopStdioError):
                relay('/trusted/codex', stdin=reader, stdout=io.BytesIO())
        self.assertLess(time.monotonic() - start, 1)

    def test_large_desktop_response_is_forwarded_without_truncation(self):
        response_size = 4404759
        prefix = b'{"id":1,"result":{"text":"'
        suffix = b'"}}'
        payload = prefix + b'x' * (response_size - len(prefix) - len(suffix)) + suffix
        self.assertEqual(len(payload), response_size)
        reader, _ = self.input_pipe()
        raw = self.proxy(lambda server, peer: self.send(server, payload))
        output = self.run_relay(raw, reader)
        self.assertEqual(len(output), response_size + 1)
        self.assertEqual(hashlib.sha256(output).hexdigest(),
                         hashlib.sha256(payload + b'\n').hexdigest())

    def test_fragmented_server_message_has_total_limit(self):
        reader, _ = self.input_pipe()
        def action(server, peer):
            self.send(server, b' ' * (MAX_FRAME_BYTES // 2), fin=0)
            self.send(server, b' ' * (MAX_FRAME_BYTES // 2 + 1),
                      websocket.ABNF.OPCODE_CONT)
        raw = self.proxy(action)
        with patch('desktop_stdio.NativeProxySocket', return_value=raw):
            with self.assertRaises(DesktopStdioError):
                relay('/trusted/codex', stdin=reader, stdout=io.BytesIO())

    def test_fragmented_server_message_and_unicode_are_preserved(self):
        reader, _ = self.input_pipe()
        encoded = '{"method":"通知","params":{"text":"行\\n次"}}'.encode()
        def action(server, peer):
            self.send(server, encoded[:17], fin=0)
            self.send(server, encoded[17:], websocket.ABNF.OPCODE_CONT)
        raw = self.proxy(action)
        self.assertEqual(json.loads(self.run_relay(raw, reader)), json.loads(encoded))

    def test_truncated_server_frame_is_an_error(self):
        reader, _ = self.input_pipe()
        raw = self.proxy(lambda server, peer: peer.sendall(b'\x81\x05{'))
        with patch('desktop_stdio.NativeProxySocket', return_value=raw):
            with self.assertRaises(DesktopStdioError):
                relay('/trusted/codex', stdin=reader, stdout=io.BytesIO())

    def test_stdin_exact_wire_limit_is_forwarded_unmodified(self):
        observed = []
        received = threading.Event()
        text = b' {"id":"exact","method":"fixture"}'
        line = text + b' ' * (MAX_FRAME_BYTES - len(text) - 1) + b'\n'
        def action(server, peer):
            observed.append(server.recv())
            received.set()
        # recv still strictly decodes UTF-8; avoid its duplicate Python scan.
        raw = self.proxy(action, skip_utf8_validation=True)
        reader, writer = self.input_pipe()
        def produce():
            # Keep stdin open until the full frame is observed. EOF cancels the
            # relay and must not race a still-buffered proxy delivery.
            pending = memoryview(line)
            while pending:
                pending = pending[writer.write(pending):]
            received.wait(10)
            writer.close()
        producer = threading.Thread(target=produce)
        self.threads.append(producer)
        producer.start()
        self.run_relay(raw, reader)
        self.assertTrue(received.is_set(), (len(observed), self.errors))
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(observed[0]), len(line) - 1)
        self.assertEqual(hashlib.sha256(observed[0].encode()).hexdigest(),
                         hashlib.sha256(line[:-1]).hexdigest())

    def test_stdout_backpressure_preserves_all_bytes_and_restores_pipe_mode(self):
        from desktop_stdio import _OutputWriter, _write_all

        reader, destination = self.input_pipe()
        output = _OutputWriter(destination)
        original_mode = output.previous_mode
        stopped = threading.Event()
        deadline = threading.Timer(5, stopped.set)
        payload = ('{"text":"通知\\n次"}\n'.encode() * 8192)
        received = bytearray()
        errors = []
        def consume():
            try:
                time.sleep(0.15)
                while True:
                    chunk = reader.read(4096)
                    if not chunk:
                        return
                    received.extend(chunk)
            except Exception as exc:
                errors.append(exc)
        consumer = threading.Thread(target=consume)
        consumer.start()
        deadline.start()
        try:
            self.assertTrue(_write_all(output, payload, stopped))
        finally:
            deadline.cancel()
            deadline.join()
            try:
                output.close()
                restored = _OutputWriter(destination)
                try:
                    restored_mode = restored.previous_mode
                finally:
                    restored.close()
            finally:
                destination.close()
                consumer.join(2)
        self.assertEqual(restored_mode, original_mode)
        self.assertFalse(consumer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(bytes(received), payload)

    def test_stdout_zero_write_fails_and_unblocks_stdin(self):
        class ZeroWriter(io.BytesIO):
            def write(self, value):
                return 0
        reader, _ = self.input_pipe()
        raw = self.proxy(lambda server, peer: self.send(server, '{}'))
        with patch('desktop_stdio.NativeProxySocket', return_value=raw):
            with self.assertRaises(DesktopStdioError) as caught:
                relay('/trusted/codex', stdin=reader, stdout=ZeroWriter())
        self.assertEqual(caught.exception.reason, 'stdout_write')
        self.assertEqual(raw.fileno(), -1)

    def test_output_failure_reason_survives_sender_cleanup_error(self):
        for reason in ('websocket_frame', 'stdout_write'):
            with self.subTest(reason=reason):
                class Raw:
                    def close(self):
                        pass
                class WS:
                    def connect(self, *args, **kwargs):
                        pass
                    def settimeout(self, value):
                        pass
                    def recv_data(self):
                        payload = b'[]' if reason == 'websocket_frame' else b'{}'
                        return websocket.ABNF.OPCODE_TEXT, payload
                    def abort(self):
                        raise OSError(10057, 'private-cleanup-detail')
                class ZeroWriter(io.BytesIO):
                    def write(self, value):
                        return 0
                def wait_for_stop(reader, size, stopped):
                    if not stopped.wait(1):
                        raise AssertionError('output did not stop')
                    return None
                with patch('desktop_stdio.NativeProxySocket', return_value=Raw()), patch(
                        'desktop_stdio._BoundedWebSocket', return_value=WS()), patch(
                        'desktop_stdio._InputReader.read', new=wait_for_stop):
                    with self.assertRaises(DesktopStdioError) as caught:
                        relay('/fixture/private-path', stdin=io.BytesIO(), stdout=ZeroWriter())
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn('private', str(caught.exception))

    def test_input_failure_reason_survives_websocket_abort_error(self):
        aborted = threading.Event()
        receiving = threading.Event()
        class Raw:
            def close(self):
                pass
        class WS:
            def connect(self, *args, **kwargs):
                pass
            def settimeout(self, value):
                pass
            def recv_data(self):
                receiving.set()
                aborted.wait(1)
                raise OSError('private-websocket-detail')
            def abort(self):
                aborted.set()
        def fail_read(*args):
            if not receiving.wait(1):
                raise AssertionError('receiver did not start')
            raise OSError('private-input-detail')
        with patch('desktop_stdio.NativeProxySocket', return_value=Raw()), patch(
                'desktop_stdio._BoundedWebSocket', return_value=WS()), patch(
                'desktop_stdio._InputReader.read', side_effect=fail_read):
            with self.assertRaises(DesktopStdioError) as caught:
                relay('/fixture/private-path', stdin=io.BytesIO(), stdout=io.BytesIO())
        self.assertEqual(caught.exception.reason, 'stdin_read')
        self.assertTrue(receiving.is_set())
        self.assertNotIn('private', str(caught.exception))

    def test_websocket_send_read_and_cleanup_failures_have_fixed_reasons(self):
        for reason in ('websocket_send', 'websocket_read', 'cleanup'):
            with self.subTest(reason=reason):
                aborted = threading.Event()
                class Raw:
                    def close(self):
                        if reason == 'cleanup':
                            raise OSError('private-cleanup-detail')
                class WS:
                    def connect(self, *args, **kwargs):
                        pass
                    def settimeout(self, value):
                        pass
                    def send(self, text):
                        raise OSError('private-send-detail')
                    def recv_data(self):
                        if reason == 'websocket_read':
                            raise OSError('private-read-detail')
                        aborted.wait(1)
                        return websocket.ABNF.OPCODE_CLOSE, b''
                    def abort(self):
                        aborted.set()
                source = io.BytesIO(b'{}\n') if reason == 'websocket_send' else self.input_pipe()[0]
                if reason == 'cleanup':
                    aborted.set()
                with patch('desktop_stdio.NativeProxySocket', return_value=Raw()), patch(
                        'desktop_stdio._BoundedWebSocket', return_value=WS()):
                    with self.assertRaises(DesktopStdioError) as caught:
                        relay('/fixture/private-path', stdin=source, stdout=io.BytesIO())
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn('private', str(caught.exception))

    def test_diagnostic_reason_accepts_only_fixed_enum(self):
        for reason in ('private-body', None, ['stdin_read']):
            with self.subTest(reason=reason):
                error = DesktopStdioError('compatible message', reason=reason)
                self.assertEqual(str(error), 'compatible message')
                self.assertIn(error.reason, DesktopStdioError.REASONS)
                self.assertNotEqual(error.reason, 'private-body')

    def test_invalid_timeout_does_not_create_proxy(self):
        for timeout in (None, True, 0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), patch('desktop_stdio.NativeProxySocket') as native:
                with self.assertRaises(DesktopStdioError):
                    relay('/trusted/codex', stdin=io.BytesIO(), stdout=io.BytesIO(), timeout=timeout)
                native.assert_not_called()

    def test_connection_failure_is_sanitized_and_proxy_closed(self):
        raw, peer = socket.socketpair()
        self.sockets.extend([raw, peer])
        with patch('desktop_stdio.NativeProxySocket', return_value=raw):
            with patch('desktop_stdio._BoundedWebSocket.connect', side_effect=RuntimeError('private')):
                with self.assertRaises(DesktopStdioError) as caught:
                    relay('/trusted/codex', stdin=io.BytesIO(), stdout=io.BytesIO())
        self.assertNotIn('private', str(caught.exception))
        self.assertEqual(raw.fileno(), -1)


if __name__ == '__main__':
    unittest.main()
