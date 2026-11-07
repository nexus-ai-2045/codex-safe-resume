import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from unittest.mock import Mock, patch

import queue_transport

THREAD = '01a161ab-e82d-7631-a8aa-d33c77b2128e'
OTHER = '01a161ab-e82d-7631-a8aa-d33c77b2128f'
ATTEMPT = '01a161ab-e82d-7631-a8aa-d33c77b21280'
QUEUE = '01a161ab-e82d-7631-a8aa-d33c77b21281'


class Capture(io.BytesIO):
    def close(self):
        self.captured = self.getvalue()
        super().close()


def child(data, status=0):
    process = Mock(stdin=Capture(), stdout=io.BytesIO(data))
    process.wait.return_value = status
    process.poll.return_value = status
    process.returncode = status
    return process


class QueueTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'codex.exe'
        magic = b'MZ' if os.name == 'nt' else (b'\xcf\xfa\xed\xfe' if sys.platform == 'darwin' else b'\x7fELF')
        self.path.write_bytes(magic + b'fixture')
        self.location = patch('connection_settings.validate_location', side_effect=lambda p: Path(p))
        self.location.start()
        self.addCleanup(self.location.stop)
        self.transport = queue_transport.QueueTransport(self.path, self.temp.name, timeout=1)

    def ready(self):
        with patch.object(self.transport, '_output', side_effect=[
                (0, b'codex-cli 0.153.4\n'),
                (0, b'Usage: codex queue [OPTIONS] --thread <THREAD> --message <MESSAGE>\n')]):
            self.assertTrue(self.transport.check())

    def submit(self, output=None, **kwargs):
        self.ready()
        process = child(output if output is not None else
                        f'Queued message {QUEUE} for thread {THREAD}.\n'.encode())
        with patch('queue_transport.subprocess.Popen', return_value=process) as spawn:
            result = self.transport.submit(THREAD, '--literal "quoted" $(echo x)', ATTEMPT, **kwargs)
        return result, process, spawn

    def test_requires_check_and_valid_canonical_identifiers(self):
        with patch('queue_transport.subprocess.Popen') as spawn:
            for thread, attempt in ((THREAD, ATTEMPT), ('--bad', ATTEMPT), (THREAD.upper(), ATTEMPT),
                                    (THREAD, 'bad')):
                self.assertEqual(self.transport.submit(thread, 'continue', attempt)['outcome'], 'not_started')
            spawn.assert_not_called()

    def test_receipt_is_queue_not_turn_and_literal_is_one_argument(self):
        result, process, spawn = self.submit()
        self.assertEqual(result, {'outcome': 'accepted', 'queue_id': QUEUE})
        self.assertNotIn('turn_id', result)
        self.assertEqual(spawn.call_count, 1)
        args, options = spawn.call_args
        self.assertEqual(args[0], [str(self.path), 'queue', '--thread', THREAD,
                                  '--message=--literal "quoted" $(echo x)'])
        self.assertFalse(options['shell'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertTrue(process.stdout.closed)

    def test_wrong_thread_bad_receipt_nonzero_are_unknown(self):
        for payload in (f'Queued message {QUEUE} for thread {OTHER}.\n'.encode(), b'error\n',
                        f'Queued message bad for thread {THREAD}.\n'.encode()):
            result, _, spawn = self.submit(payload)
            self.assertEqual(result['outcome'], 'unknown')
            self.assertEqual(spawn.call_count, 1)
        self.ready()
        with patch('queue_transport.subprocess.Popen', return_value=child(
                f'Queued message {QUEUE} for thread {THREAD}.\n'.encode(), 1)):
            self.assertEqual(self.transport.submit(THREAD, 'x', ATTEMPT)['outcome'], 'unknown')

    def test_callback_refusal_exception_and_fingerprint_change_never_spawn(self):
        self.ready()
        with patch('queue_transport.subprocess.Popen') as spawn:
            for guard in (lambda: False, Mock(side_effect=ValueError('private'))):
                self.assertEqual(self.transport.submit(THREAD, 'x', ATTEMPT, guard)['outcome'], 'not_started')
            self.path.write_bytes(b'MZchanged')
            self.assertEqual(self.transport.submit(THREAD, 'x', ATTEMPT)['outcome'], 'not_started')
            spawn.assert_not_called()

    def test_timeout_is_unknown_and_only_own_child_is_reaped(self):
        self.ready()
        process = child(b'')
        process.wait.side_effect = [subprocess.TimeoutExpired('private', .1), 0]
        with patch('queue_transport.subprocess.Popen', return_value=process) as spawn:
            result = self.transport.submit(THREAD, 'x', ATTEMPT)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertEqual(spawn.call_count, 1)
        self.assertTrue(process.stdout.closed)

    def test_environment_removes_relay_injection_without_mutating_parent(self):
        with patch.dict('os.environ', {'CODEX_CLI_PATH': 'secret', 'CSR_WINDOWS_ORDER_FREE': '1',
                                      'CODEX_APP_SERVER_FORCE_CLI': '1', 'UNRELATED': 'keep'}):
            result, _, spawn = self.submit()
            env = spawn.call_args.kwargs['env']
            self.assertEqual(result['outcome'], 'accepted')
            self.assertEqual(env['CODEX_HOME'], self.temp.name)
            self.assertEqual(env['UNRELATED'], 'keep')
            self.assertNotIn('CODEX_CLI_PATH', env)
            self.assertNotIn('CSR_WINDOWS_ORDER_FREE', env)
            self.assertNotIn('CODEX_APP_SERVER_FORCE_CLI', env)
            import os
            self.assertEqual(os.environ['CODEX_CLI_PATH'], 'secret')

    def test_check_rejects_wrappers_bad_version_or_missing_flags(self):
        for payloads in ([(0, b'not codex\n')], [(0, b'codex-cli 1.2.3\n'), (0, b'queue help')]):
            with patch.object(self.transport, '_output', side_effect=payloads):
                self.assertFalse(self.transport.check())
        self.path.write_bytes(b'#!/bin/sh\ncodex')
        with patch.object(self.transport, '_output') as probe:
            self.assertFalse(self.transport.check())
            probe.assert_not_called()

    def test_oversized_receipt_is_unknown(self):
        result, _, _ = self.submit(b'x' * 70000 + b'\n')
        self.assertEqual(result['outcome'], 'unknown')

    def rpc(self, method='account/read', params=None, result=None):
        self.ready()
        process = child(b''.join(json.dumps(item).encode() + b'\n' for item in (
            {'id': 1, 'result': {'userAgent': 'codex'}},
            {'id': 2, 'result': result or {'ok': True}})))
        with patch('queue_transport.subprocess.Popen', return_value=process) as spawn:
            value = self.transport.rpc(method, {} if params is None else params)
        return value, process, spawn

    def test_rpc_experimental_initialization_and_allowlist(self):
        value, process, _ = self.rpc()
        self.assertEqual(value, {'ok': True})
        sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertEqual([x['method'] for x in sent], ['initialize', 'initialized', 'account/read'])
        self.assertTrue(sent[0]['params']['capabilities']['experimentalApi'])
        with patch('queue_transport.subprocess.Popen') as spawn:
            for method, params in (('turn/start', {}), ('thread/resume', {}),
                                   ('account/read', {'refreshToken': True}),
                                   ('thread/queue/delete', {'threadId': THREAD, 'queuedSubmissionId': QUEUE})):
                with self.assertRaises(queue_transport.QueueTransportError):
                    self.transport.rpc(method, params)
            spawn.assert_not_called()

    def test_cancel_requires_own_accepted_queue_and_same_thread(self):
        self.submit()
        value, process, _ = self.rpc('thread/queue/delete', {'threadId': THREAD, 'queuedSubmissionId': QUEUE},
                                     {'deleted': True})
        self.assertTrue(value['deleted'])
        with patch('queue_transport.subprocess.Popen') as spawn:
            for params in ({'threadId': OTHER, 'queuedSubmissionId': QUEUE},
                           {'threadId': THREAD, 'queuedSubmissionId': OTHER},
                           {'threadId': THREAD, 'queuedSubmissionId': 'bad'}):
                with self.assertRaises(queue_transport.QueueTransportError):
                    self.transport.rpc('thread/queue/delete', params)
            spawn.assert_not_called()

    def enqueue(self, submission=None, guard=None, messages=None):
        self.ready()
        if messages is None:
            messages = [{'id': 1, 'result': {'userAgent': 'codex'}},
                        {'id': 2, 'result': {'queuedSubmission': submission or {'id': QUEUE}}}]
        process = child(b''.join(json.dumps(item).encode() + b'\n' for item in messages))
        with patch('queue_transport.subprocess.Popen', return_value=process) as spawn:
            value = self.transport.enqueue(THREAD, '続けて', ATTEMPT, before_send=guard)
        return value, process, spawn

    def test_enqueue_sets_exact_client_id_without_body_marker(self):
        value, process, spawn = self.enqueue()
        self.assertEqual(value, {'outcome': 'accepted', 'queue_id': QUEUE})
        self.assertEqual(spawn.call_count, 1)
        sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertEqual(sent[-1], {'id': 2, 'method': 'thread/queue/add', 'params': {
            'threadId': THREAD, 'input': [{'type': 'text', 'text': '続けて'}],
            'clientUserMessageId': ATTEMPT}})
        self.assertNotIn('turn_id', value)
        with patch('queue_transport.subprocess.Popen') as spawn:
            with self.assertRaises(queue_transport.QueueTransportError):
                self.transport.rpc('thread/queue/add', sent[-1]['params'])
            spawn.assert_not_called()

    def test_enqueue_guard_is_after_init_and_before_add(self):
        for guard in (lambda: False, Mock(side_effect=RuntimeError('secret'))):
            value, process, spawn = self.enqueue(guard=guard)
            self.assertEqual(value['outcome'], 'not_started')
            sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
            self.assertEqual([item['method'] for item in sent], ['initialize', 'initialized'])
            self.assertEqual(spawn.call_count, 1)

    def test_enqueue_safety_check_has_its_own_time_before_submission(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                clock = [100.0]

                def guard():
                    clock[0] += self.transport.timeout + 1
                    return allowed

                with patch('queue_transport.time.monotonic', side_effect=lambda: clock[0]):
                    value, process, spawn = self.enqueue(guard=guard)
                self.assertEqual(value, {'outcome': 'accepted', 'queue_id': QUEUE}
                                 if allowed else {'outcome': 'not_started'})
                sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
                self.assertEqual([item['method'] for item in sent],
                                 ['initialize', 'initialized', 'thread/queue/add']
                                 if allowed else ['initialize', 'initialized'])
                self.assertEqual(spawn.call_count, 1)

    def test_enqueue_response_after_slow_guard_is_still_bounded(self):
        clock = [100.0]
        response = queue_transport._response

        def guard():
            clock[0] += self.transport.timeout + 1
            return True

        def expired_response(incoming, failed, request_id, deadline):
            if request_id == 2:
                clock[0] += self.transport.timeout + 1
            return response(incoming, failed, request_id, deadline)

        with patch('queue_transport.time.monotonic', side_effect=lambda: clock[0]), \
                patch('queue_transport._response', side_effect=expired_response):
            value, process, spawn = self.enqueue(guard=guard)
        self.assertEqual(value, {'outcome': 'unknown'})
        sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertEqual([item['method'] for item in sent],
                         ['initialize', 'initialized', 'thread/queue/add'])
        self.assertEqual(spawn.call_count, 1)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stdin.closed)
        self.assertFalse(self.transport._owned)

    def test_enqueue_bad_reply_is_unknown_never_retried(self):
        for submission in ({'id': 'bad'}, {'id': QUEUE, 'threadId': OTHER},
                           {'id': QUEUE, 'clientUserMessageId': OTHER}):
            value, _, spawn = self.enqueue(submission=submission)
            self.assertEqual(value['outcome'], 'unknown')
            self.assertEqual(spawn.call_count, 1)
        value, _, spawn = self.enqueue(messages=[{'id': 1, 'result': {'userAgent': 'codex'}},
                                                 {'id': 2, 'error': {'message': 'private'}}])
        self.assertEqual(value['outcome'], 'unknown')
        self.assertEqual(spawn.call_count, 1)

    def test_enqueue_initialization_failure_is_not_started(self):
        value, process, _ = self.enqueue(messages=[{'id': 1, 'error': {'message': 'private'}}])
        self.assertEqual(value['outcome'], 'not_started')
        sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertEqual([item['method'] for item in sent], ['initialize'])

    def test_full_multiline_help_accepts_collector_finishing_before_consumer(self):
        help_text = b'Usage: codex queue [OPTIONS] --thread <THREAD> --message <MESSAGE>\n'
        help_text += b'  --fixture option description\n' * 120
        processes = [child(b'codex-cli 0.153.4\n'), child(help_text)]
        with patch('queue_transport.subprocess.Popen', side_effect=processes), \
                patch('queue_transport.threading.Thread.start', lambda thread: thread.run()):
            self.assertTrue(self.transport.check())
        self.assertTrue(all(process.stdout.closed for process in processes))

    def test_multiline_plain_output_remains_limited_to_64_kib(self):
        for data in (b'x' * 65537 + b'\n', b'\n' * 65537):
            process = child(data)
            with patch('queue_transport.subprocess.Popen', return_value=process), \
                    patch('queue_transport.threading.Thread.start', lambda thread: thread.run()):
                with self.assertRaises(ValueError):
                    self.transport._output(['queue', '--help'])
            self.assertTrue(process.stdout.closed)

    def test_source_change_during_check_is_rejected(self):
        def probe(arguments):
            if arguments == ['--version']:
                return 0, b'codex-cli 1.2.3'
            self.path.write_bytes(self.path.read_bytes() + b'changed')
            return 0, b'Usage: codex queue --thread X --message Y'
        with patch.object(self.transport, '_output', side_effect=probe):
            self.assertFalse(self.transport.check())
        with patch('queue_transport.subprocess.Popen') as spawn:
            self.assertEqual(self.transport.submit(THREAD, 'x', ATTEMPT)['outcome'], 'not_started')
            spawn.assert_not_called()

    def test_invalid_timeout_is_fixed_error(self):
        for timeout in (True, 0, -1, float('inf'), float('nan'), '8', 121):
            with self.assertRaises(queue_transport.QueueTransportError):
                queue_transport.QueueTransport(self.path, self.temp.name, timeout)

    def test_writer_block_is_bounded_and_owned_child_only_is_stopped(self):
        self.ready()
        released = threading.Event()
        process = child(b'')
        def blocked_write(data):
            released.wait(3)
            return len(data)
        process.stdin.write = blocked_write
        process.terminate.side_effect = released.set
        self.transport.timeout = .05
        started = time.monotonic()
        try:
            with patch('queue_transport.subprocess.Popen', return_value=process) as spawn:
                value = self.transport.enqueue(THREAD, 'x', ATTEMPT)
            self.assertEqual(value['outcome'], 'not_started')
            self.assertEqual(spawn.call_count, 1)
            process.terminate.assert_called_once()
            self.assertLess(time.monotonic() - started, 2)
        finally:
            released.set()

    def test_unterminated_cli_output_and_cleanup_error_stay_unknown(self):
        result, _, _ = self.submit(f'Queued message {QUEUE} for thread {THREAD}.'.encode())
        self.assertEqual(result['outcome'], 'unknown')
        self.ready()
        process = child(f'Queued message {QUEUE} for thread {THREAD}.\n'.encode())
        with patch('queue_transport.subprocess.Popen', return_value=process), \
                patch('queue_transport._close', side_effect=OSError('private-path')):
            value = self.transport.submit(THREAD, 'x', ATTEMPT)
        self.assertEqual(value['outcome'], 'unknown')

    def test_rpc_failure_is_fixed_and_has_no_raw_error(self):
        self.ready()
        process = child(b'{"id":1,"error":{"message":"private-account private-path"}}\n')
        with patch('queue_transport.subprocess.Popen', return_value=process):
            with self.assertRaises(queue_transport.QueueTransportError) as caught:
                self.transport.rpc('account/read', {})
        self.assertNotIn('private', str(caught.exception))
        self.assertTrue(process.stdout.closed)


if __name__ == '__main__':
    unittest.main()

