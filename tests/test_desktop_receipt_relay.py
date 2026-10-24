"""ネットワークや実processを使わずstdioの証明発行順を確認する。"""
import io
import json
import threading
import unittest
from unittest.mock import Mock, patch
import websocket
import desktop_stdio
from desktop_binding import Handshake


class ReceiptRelayTests(unittest.TestCase):
    def run_relay(self, fail_write=False, fail_send=False, pause_delivery=False, premature=False):
        initialized = threading.Event()
        response_written = threading.Event()
        request_sent = threading.Event()
        observations = []

        class Receipt(Handshake):
            def forwarded(self, message):
                super().forwarded(message)
                observations.append(('forwarded', message.get('method')))
                if message.get('method') == 'initialized':
                    initialized.set()

            def delivered(self, message):
                observations.append(('delivered', len(output.getvalue())))
                if pause_delivery:
                    initialized.wait(.1)
                super().delivered(message)

            def close(self):
                observations.append(('close', self.complete))

        receipt = Receipt()
        response = json.dumps({'id': 1, 'result': {'userAgent': 'Desktop'}}).encode()

        class Output(io.BytesIO):
            def write(self, value):
                if fail_write:
                    raise OSError('write failed')
                count = super().write(value[:3])
                if self.getvalue().endswith(b'\n'):
                    response_written.set()
                return count

            def close(self):
                pass

        output = Output()
        messages = [b'{"id":1,"method":"initialize"}\n', b'{"method":"initialized"}\n']
        reads = iter([b''.join(messages)] if premature else messages)
        def read(_limit, stopped):
            if request_sent.is_set():
                if not response_written.wait(.5):
                    return None
            try:
                return next(reads)
            except StopIteration:
                stopped.wait(.01)
                return None

        ws = Mock()
        def send(_text):
            if fail_send:
                raise OSError('send failed')
            request_sent.set()
        ws.send.side_effect = send
        recv_count = 0
        def receive():
            nonlocal recv_count
            recv_count += 1
            request_sent.wait(.5)
            if premature:
                initialized.wait(.5)
            if recv_count == 1:
                return websocket.ABNF.OPCODE_TEXT, response
            initialized.wait(.5)
            return websocket.ABNF.OPCODE_CLOSE, b''
        ws.recv_data.side_effect = receive
        with patch.object(desktop_stdio, 'NativeProxySocket'), \
                patch.object(desktop_stdio, '_InputReader') as reader, \
                patch.object(desktop_stdio, '_OutputWriter', return_value=output), \
                patch.object(desktop_stdio, '_BoundedWebSocket', return_value=ws):
            reader.return_value.read.side_effect = read
            try:
                desktop_stdio.relay('/fixture/cli', stdin=io.BytesIO(), stdout=io.BytesIO(), receipt=receipt)
            except desktop_stdio.DesktopStdioError:
                if not (fail_write or fail_send):
                    raise
        return observations, response

    def test_full_short_writes_precede_delivery_proof(self):
        events, response = self.run_relay()
        self.assertIn(('delivered', len(response)+1), events)
        self.assertIn(('close', True), events)
        self.assertLess(events.index(('forwarded', 'initialize')),
                        events.index(('delivered', len(response)+1)))

    def test_failed_stdout_cannot_attest(self):
        events, _ = self.run_relay(fail_write=True)
        self.assertFalse(any(event[0] == 'delivered' for event in events))
        self.assertIn(('close', False), events)

    def test_failed_ws_send_cannot_attest_request(self):
        events, _ = self.run_relay(fail_send=True)
        self.assertNotIn(('forwarded', 'initialize'), events)
        self.assertIn(('close', False), events)

    def test_initialized_after_full_write_before_delivered_remains_valid(self):
        events, _ = self.run_relay(pause_delivery=True)
        self.assertIn(('close', True), events)

    def test_initialized_before_response_is_not_retroactively_accepted(self):
        events, _ = self.run_relay(premature=True)
        self.assertIn(('close', False), events)

    def test_stdout_backpressure_does_not_hold_handshake_lock(self):
        receipt = Handshake()
        pending_write = threading.Event()
        acquired = threading.Event()
        stopped = threading.Event()
        output = Mock()
        def write(_pending):
            pending_write.set()
            return None
        output.write.side_effect = write
        def reader():
            pending_write.wait(1)
            with receipt.lock:
                acquired.set()
                stopped.set()
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertFalse(desktop_stdio._write_all(
                output, b'line\n', stopped, receipt=receipt, message={}))
            self.assertTrue(acquired.is_set())
        finally:
            stopped.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
