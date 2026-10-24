"""表示専用RPCの固定操作・不正値拒否・所有プロセスの回収を検証する。"""
import io
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import usage_client


def reply(**changes):
    value = {'accountId': 'private-account', 'ordinaryUsageAllowed': False,
             'rateLimits': {'primary': {'usedPercent': 75, 'windowDurationMins': 10080,
                                        'resetsAt': 1900000000}}}
    value.update(changes)
    return value


def wire(result=None, *, messages=None):
    messages = messages if messages is not None else [
        {'id': 1, 'result': {'userAgent': 'fixture'}},
        {'id': 2, 'result': reply() if result is None else result}]
    return b''.join(json.dumps(item).encode() + b'\n' for item in messages)


class Capture(io.BytesIO):
    def close(self):
        self.captured = self.getvalue()
        super().close()


class UsageClientTests(unittest.TestCase):
    def read(self, payload=None, *, result=None, process=None, timeout=1, validated_path=None,
             peer_reply=None):
        process = process or Mock(stdin=Capture(), stdout=io.BytesIO(
            wire(result) if payload is None else payload))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch('usage_client.validate_location', return_value=validated_path or Path('/fixture/codex')), \
                patch('usage_client.subprocess.Popen', return_value=process) as spawn:
            value = usage_client.read_limits('/fixture/codex', timeout=timeout, peer_reply=peer_reply)
        return value, process, spawn

    def test_null_multi_bucket_uses_valid_legacy_codex_snapshot(self):
        result = reply(rateLimitsByLimitId=None)
        value, _, _ = self.read(result=result)
        self.assertEqual(value['windows'][0]['duration_minutes'], 10080)

    def test_nullable_limit_id_is_valid_in_selected_codex_and_legacy_views(self):
        snapshot = dict(reply()['rateLimits'], limitId=None)
        for result in (reply(rateLimitsByLimitId={'codex': snapshot}, rateLimits=None),
                       reply(rateLimits=snapshot),
                       reply(rateLimitsByLimitId=None, rateLimits=snapshot)):
            with self.subTest(multi_view=result.get('rateLimitsByLimitId')):
                value, _, _ = self.read(result=result)
                self.assertEqual(value['windows'][0]['duration_minutes'], 10080)

    def test_validated_posix_and_windows_paths_are_preserved(self):
        for path in (PurePosixPath('/fixture/codex'), PureWindowsPath('C:/fixture/codex.exe')):
            with self.subTest(style=type(path).__name__):
                _, _, spawn = self.read(validated_path=path)
                self.assertEqual(spawn.call_args.args[0], [str(path), 'app-server', '--listen', 'stdio://'])

    def test_primary_weekly_only_is_not_invented_as_five_hour(self):
        value, process, spawn = self.read()
        self.assertEqual(value['windows'], [dict(slot='primary', duration_minutes=10080,
                         used_percent=75, remaining_percent=25, resets_at=1900000000)])
        self.assertTrue(value['account_bound'])
        self.assertIs(value['ordinary_usage_allowed'], False)
        self.assertIsInstance(value['checked_at'], int)
        self.assertNotIn('private-account', json.dumps(value))
        self.assertEqual(spawn.call_args.args[0], [str(Path('/fixture/codex')), 'app-server', '--listen', 'stdio://'])
        self.assertFalse(spawn.call_args.kwargs['shell'])
        self.assertEqual(spawn.call_args.kwargs['stderr'], subprocess.DEVNULL)
        sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertEqual([item['method'] for item in sent], ['initialize', 'initialized', 'account/rateLimits/read'])
        self.assertEqual([item.get('id') for item in sent], [1, None, 2])
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        process.wait.assert_called()

    def test_codex_map_is_authoritative_and_other_buckets_never_fall_back(self):
        selected = reply()['rateLimits']
        selected['limitId'] = 'codex'
        value, _, _ = self.read(result=reply(rateLimitsByLimitId={'codex': selected}, rateLimits=None))
        self.assertEqual(len(value['windows']), 1)
        for mapping in ({}, {'other': selected}, {'codex': None}, 'invalid-map'):
            with self.subTest(mapping=mapping), self.assertRaises(usage_client.UsageReadError):
                self.read(result=reply(rateLimitsByLimitId=mapping))

    def test_legacy_shape_and_bucket_binding_are_checked(self):
        for bucket in (None, {}, {'limitId': 'other', 'primary': reply()['rateLimits']['primary']}):
            with self.subTest(bucket=bucket), self.assertRaises(usage_client.UsageReadError):
                self.read(result=reply(rateLimits=bucket))
        with self.assertRaises(usage_client.UsageReadError):
            self.read(result={'primary': reply()['rateLimits']['primary']})
        with self.assertRaises(usage_client.UsageReadError):
            self.read(result={'rateLimitsByLimitId': None,
                              'primary': reply()['rateLimits']['primary']})

    def test_null_duration_and_reset_remain_unknown_and_past_reset_is_not_rewritten(self):
        window = dict(usedPercent=12.5, windowDurationMins=None, resetsAt=None)
        value, _, _ = self.read(result=reply(rateLimits={'secondary': window}, accountId=None,
                                            ordinaryUsageAllowed='unknown'))
        self.assertEqual(value['windows'], [dict(slot='secondary', duration_minutes=None,
                         used_percent=12.5, remaining_percent=87.5, resets_at=None)])
        self.assertFalse(value['account_bound'])
        self.assertIsNone(value['ordinary_usage_allowed'])
        window.update(windowDurationMins=300, resetsAt=1700000000)
        value, _, _ = self.read(result=reply(rateLimits={'primary': window}))
        self.assertEqual(value['windows'][0]['resets_at'], 1700000000)

    def test_malformed_values_fail_without_raw_reply_or_paths(self):
        cases = {'usedPercent': [True, -1, 101, '1', None, float('nan'), float('inf')],
                 'windowDurationMins': [True, 0, -1, 1.5, '300', float('inf'), 10**12],
                 'resetsAt': [True, 0, -1, 1.5, 'private-secret', 10**30]}
        for field, values in cases.items():
            for value in values:
                window = dict(reply()['rateLimits']['primary'], **{field: value})
                with self.subTest(field=field, value=value):
                    with self.assertRaises(usage_client.UsageReadError) as caught:
                        self.read(result=reply(rateLimits={'primary': window}))
                    self.assertNotIn('private-secret', str(caught.exception))
                    self.assertNotIn('/fixture/', str(caught.exception))

    def test_server_request_is_never_answered(self):
        process = Mock(stdin=Capture(), stdout=io.BytesIO(wire(messages=[
            {'id': 1, 'result': {'userAgent': 'fixture'}},
            {'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}}])))
        with self.assertRaises(usage_client.UsageReadError):
            self.read(process=process)
        outbound = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertFalse(any('result' in item or 'error' in item for item in outbound))
        self.assertTrue(all(item['method'] in ('initialize', 'initialized', 'account/rateLimits/read') for item in outbound))

    def test_eof_invalid_json_error_and_oversized_line_are_sanitized(self):
        for data in (b'', b'private-body\n', b'{}', b'[]\n',
                     wire(messages=[{'id': 1, 'error': {'message': 'private-body'}}]),
                     b'x' * (4 * 1024 * 1024 + 1)):
            with self.subTest(length=len(data)), self.assertRaises(usage_client.UsageReadError) as caught:
                self.read(data)
            self.assertNotIn('private-body', str(caught.exception))

    def test_permitted_notification_bursts_survive_reader_scheduling(self):
        # BytesIO lets the producer finish before the consumer is scheduled.
        notifications = [{'method': 'notification', 'params': {}}] * 32
        messages = (notifications + [{'id': 1, 'result': {'userAgent': 'fixture'}}]
                    + notifications + [{'id': 2, 'result': reply()}])
        value, process, _ = self.read(wire(messages=messages))
        self.assertEqual(value['windows'][0]['duration_minutes'], 10080)
        self.assertTrue(process.stdout.closed)
        self.assertFalse(any(t.name == 'usage-limits-reader' for t in threading.enumerate()))

    def test_queue_or_notification_flood_is_bounded(self):
        data = wire(messages=[{'method': 'notification', 'params': {}}] * 1000)
        with self.assertRaises(usage_client.UsageReadError):
            self.read(data)

    def test_timeout_and_real_fixture_process_are_reaped_without_reader_leak(self):
        real_spawn = subprocess.Popen
        owned = []
        def spawn(*args, **kwargs):
            process = real_spawn([sys.executable, '-u', '-c', 'import time; time.sleep(10)'], **kwargs)
            owned.append(process)
            return process
        with patch('usage_client.validate_location', return_value=Path('/fixture/codex')), \
                patch('usage_client.subprocess.Popen', side_effect=spawn):
            with self.assertRaises(usage_client.UsageReadError):
                usage_client.read_limits('/fixture/codex', timeout=0.05)
        self.assertIsNotNone(owned[0].poll())
        self.assertTrue(owned[0].stdin.closed)
        self.assertTrue(owned[0].stdout.closed)
        self.assertFalse(any(t.name == 'usage-limits-reader' for t in threading.enumerate()))

    def test_invalid_timeout_and_location_do_not_launch_process(self):
        for timeout in (None, True, 0, -1, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), patch('usage_client.subprocess.Popen') as spawn:
                with self.assertRaises(usage_client.UsageReadError):
                    usage_client.read_limits('/fixture/codex', timeout=timeout)
                spawn.assert_not_called()
        with patch('usage_client.validate_location', side_effect=ValueError('private-path')), \
                patch('usage_client.subprocess.Popen') as spawn:
            with self.assertRaises(usage_client.UsageReadError) as caught:
                usage_client.read_limits('/fixture/codex')
            self.assertNotIn('private-path', str(caught.exception))
            spawn.assert_not_called()

    def test_windows_child_has_no_console(self):
        with patch('usage_client.os', SimpleNamespace(name='nt')):
            _, _, spawn = self.read()
        self.assertEqual(spawn.call_args.kwargs['creationflags'],
                         getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000))

    def test_boot_failure_and_failed_initialization_are_sanitized(self):
        with patch('usage_client.validate_location', return_value=Path('/fixture/codex')), \
                patch('usage_client.subprocess.Popen', side_effect=OSError('private-boot-path')):
            with self.assertRaises(usage_client.UsageReadError) as caught:
                usage_client.read_limits('/fixture/codex')
            self.assertNotIn('private-boot-path', str(caught.exception))
        process = Mock(stdin=Capture(), stdout=io.BytesIO(wire(messages=[{'id': 1, 'result': {}}])))
        with self.assertRaises(usage_client.UsageReadError):
            self.read(process=process)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        process.wait.assert_called()
        sent = [json.loads(line) for line in process.stdin.captured.splitlines()]
        self.assertEqual([item['method'] for item in sent], ['initialize'])

    def test_graceful_and_terminate_timeouts_escalate_only_owned_process(self):
        process = Mock(stdin=Capture(), stdout=io.BytesIO(wire()))
        process.wait.side_effect = [subprocess.TimeoutExpired('fixture', 1),
                                    subprocess.TimeoutExpired('fixture', 1), 0]
        process.poll.side_effect = [None, None, 0]
        self.read(process=process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_dripping_unterminated_stdout_has_total_deadline(self):
        real_spawn = subprocess.Popen
        owned = []
        def spawn(*args, **kwargs):
            program = 'import os,time\nfor i in range(1000):\n os.write(1,b"x")\n time.sleep(.02)'
            process = real_spawn([sys.executable, '-u', '-c', program], **kwargs)
            owned.append(process)
            return process
        started = time.monotonic()
        with patch('usage_client.validate_location', return_value=Path('/fixture/codex')), \
                patch('usage_client.subprocess.Popen', side_effect=spawn):
            with self.assertRaises(usage_client.UsageReadError):
                usage_client.read_limits('/fixture/codex', timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(owned[0].poll())
        self.assertFalse(any(t.name == 'usage-limits-reader' for t in threading.enumerate()))

    def test_real_stdio_fixture_exchanges_only_three_fixed_messages(self):
        real_spawn = subprocess.Popen
        owned = []
        def spawn(*args, **kwargs):
            program = '''import json,sys
initialize=json.loads(sys.stdin.buffer.readline())
assert initialize['method']=='initialize' and initialize['id']==1
print(json.dumps({'id':1,'result':{'userAgent':'fixture'}}),flush=True)
initialized=json.loads(sys.stdin.buffer.readline())
assert initialized['method']=='initialized' and 'id' not in initialized
request=json.loads(sys.stdin.buffer.readline())
assert request['method']=='account/rateLimits/read' and request['id']==2
print(json.dumps({'method':'fixture/notification','params':{}}),flush=True)
print(json.dumps({'id':2,'result':RESULT}),flush=True)
assert sys.stdin.buffer.read()==b''
'''.replace('RESULT', repr(reply()))
            process = real_spawn([sys.executable, '-u', '-c', program], **kwargs)
            owned.append(process)
            return process
        with patch('usage_client.validate_location', return_value=Path('/fixture/codex')), \
                patch('usage_client.subprocess.Popen', side_effect=spawn):
            result = usage_client.read_limits('/fixture/codex', timeout=2)
        self.assertEqual(result['windows'][0]['duration_minutes'], 10080)
        self.assertEqual(owned[0].poll(), 0)
        self.assertTrue(owned[0].stdin.closed)
        self.assertTrue(owned[0].stdout.closed)


if __name__ == '__main__':
    unittest.main()
