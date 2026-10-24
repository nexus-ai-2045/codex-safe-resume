"""週次1窓は公式の明示許可だけで判定し、送信直前にも再取得する。"""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
from desktop_bridge import DesktopBridge, BridgeError
import watch


T = '123e4567-e89b-12d3-a456-426614174000'
A = '123e4567-e89b-12d3-a456-426614174001'
NOW = 1800000000


def weekly(slot='primary', **changes):
    bucket = {'limitId': None, 'primary': None, 'secondary': None,
              'spendControlReached': False, 'rateLimitReachedType': None}
    bucket[slot] = {'usedPercent': 20, 'windowDurationMins': 10080,
                    'resetsAt': NOW + 100}
    result = {'accountId': 'fixture-account', 'ordinaryUsageAllowed': True,
              'rateLimits': bucket, 'rateLimitsByLimitId': None}
    result.update(changes)
    return result


class FixtureBridge(DesktopBridge):
    def __init__(self, replies):
        super().__init__()
        self.replies = iter(replies)
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def capabilities(self): return {'desktop_binding': True, 'guarded_admission': True}
    def prepare_thread(self, thread_id): pass
    def snapshot(self, thread_id):
        if 'turn/start' in self.calls:
            return dict(id=thread_id, latest_turn='receipt', failure_turn=None,
                        limited=False, turn_status='inProgress', awaiting=False,
                        idle=False, observation_verified=True)
        return dict(id=thread_id, title='検証', cwd='', latest_turn='failure',
                    failure_turn='failure', limited=True, turn_status='failed',
                    awaiting=False, idle=True, observation_verified=True)

    def request(self, method, params=None):
        self.calls.append(method)
        if method == 'account/rateLimits/read': return next(self.replies)
        if method == 'turn/start': return {'turn': {'id': 'receipt'}}
        raise AssertionError('unexpected method')


class WeeklyPermission(unittest.TestCase):
    def test_official_weekly_window_in_either_slot_and_nullable_bucket_fields(self):
        for slot in ('primary', 'secondary'):
            for view in ('legacy', 'null', 'map'):
                for limit_id in ('omitted', None, 'codex'):
                    with self.subTest(slot=slot, view=view, limit_id=limit_id):
                        reply = weekly(slot)
                        if limit_id == 'omitted': reply['rateLimits'].pop('limitId')
                        else: reply['rateLimits']['limitId'] = limit_id
                        if view == 'legacy': reply.pop('rateLimitsByLimitId')
                        if view == 'map': reply['rateLimitsByLimitId'] = {'codex': reply['rateLimits']}
                        bridge = FixtureBridge([reply])
                        self.assertEqual(bridge.send_once(T, 'failure', attempt_id=A)['turn']['id'], 'receipt')
                        self.assertEqual(bridge.calls, ['account/rateLimits/read', 'turn/start'])

    def invalid_replies(self):
        for key, values in [('ordinaryUsageAllowed', [None, 1, 'true', 'omitted']),
                            ('accountId', [None, '', ' ', 1, 'omitted'])]:
            for value in values:
                reply = weekly()
                if value == 'omitted': reply.pop(key)
                else: reply[key] = value
                yield reply
        for key, values in [('spendControlReached', [None, True, 0, 'omitted']),
                            ('limitId', ['other']),
                            ('rateLimitReachedType', ['rate_limit_reached', 'unknown'])]:
            for value in values:
                reply = weekly()
                if value == 'omitted': reply['rateLimits'].pop(key)
                else: reply['rateLimits'][key] = value
                yield reply
        for key, values in [('usedPercent', [True, -1, 100, 101, float('nan'), float('inf')]),
                            ('windowDurationMins', [None, True, 300, 1, '10080'])]:
            for value in values:
                reply = weekly(); reply['rateLimits']['primary'][key] = value
                yield reply
        for mapping in ({}, {'other': weekly()['rateLimits']}, 'invalid'):
            yield weekly(rateLimitsByLimitId=mapping)
        direct = dict(weekly()['rateLimits'], accountId='fixture-account', ordinaryUsageAllowed=True)
        yield direct
        yield dict(direct, rateLimitsByLimitId=None)
        for primary, secondary in ((None, None), ('invalid', None),
                                   (weekly()['rateLimits']['primary'], 'invalid')):
            reply = weekly(); reply['rateLimits'].update(primary=primary, secondary=secondary)
            yield reply

    def test_unknown_or_denied_single_window_never_calls_turn_start(self):
        for index, reply in enumerate([weekly(ordinaryUsageAllowed=False), *self.invalid_replies()]):
            with self.subTest(case=index):
                bridge = FixtureBridge([reply])
                with self.assertRaises(BridgeError): bridge.send_once(T, 'failure', attempt_id=A)
                self.assertEqual(bridge.calls, ['account/rateLimits/read'])

    def tick(self, replies):
        with tempfile.TemporaryDirectory() as directory, closing(core.connection(Path(directory)/'state')) as db:
            bridge = FixtureBridge(replies)
            current = bridge.snapshot(T)
            core.sync(db, [current]); core.enable(db, T, True); core.quota(db, T, 'failure', NOW - 1)
            with patch('watch.discover', return_value=([current], None)), patch('core.time.time', return_value=NOW):
                result = watch.once(db, lambda: bridge)
            return result, core.list_threads(db)[0], bridge.calls

    def test_watch_dispatches_once_only_after_a_second_live_permission_read(self):
        result, row, calls = self.tick([weekly(), weekly()])
        self.assertEqual(result['results'], [(T, 'dispatched')])
        self.assertEqual(row['resume_turn'], 'receipt')
        self.assertEqual(calls, ['account/rateLimits/read', 'account/rateLimits/read', 'turn/start'])

    def test_permission_revoked_between_watch_and_send_never_sends(self):
        result, row, calls = self.tick([weekly(), weekly(ordinaryUsageAllowed=False)])
        self.assertEqual(row['state'], 'blocked')
        self.assertEqual(calls, ['account/rateLimits/read', 'account/rateLimits/read'])

    def test_false_permission_waits_without_making_an_attempt(self):
        result, row, calls = self.tick([weekly(ordinaryUsageAllowed=False)])
        self.assertEqual(result['state'], 'quota-wait')
        self.assertEqual(row['state'], 'waiting')
        self.assertEqual(calls, ['account/rateLimits/read'])

    def test_full_weekly_window_preserves_real_reset_and_never_sends(self):
        reply = weekly('secondary'); reply['rateLimits']['secondary']['usedPercent'] = 100
        result, row, calls = self.tick([reply])
        self.assertEqual(result['state'], 'quota-wait')
        self.assertEqual(row['reset_at'], NOW + 100)
        self.assertEqual(calls, ['account/rateLimits/read'])

    def test_direct_observation_fixture_cannot_authorize_an_rpc_send(self):
        reply = {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}
        self.assertEqual(core.observe_quota(reply, require_both=True), (True, None))
        with self.assertRaises(ValueError): core.observe_resume_quota(reply)
        bridge = FixtureBridge([reply])
        with self.assertRaises(BridgeError): bridge.send_once(T, 'failure', attempt_id=A)
        self.assertEqual(bridge.calls, ['account/rateLimits/read'])

    def test_null_rpc_map_cannot_disguise_direct_windows_as_legacy(self):
        for secondary in (None, {'usedPercent': 0}):
            reply = {'primary': {'usedPercent': 0}, 'secondary': secondary,
                     'rateLimitsByLimitId': None}
            with self.subTest(secondary=secondary):
                with self.assertRaises(ValueError): core.quota_bucket(reply)
                with self.assertRaises(ValueError): core.observe_quota(reply)
                bridge = FixtureBridge([reply])
                with self.assertRaises(BridgeError): bridge.send_once(T, 'failure', attempt_id=A)
                self.assertEqual(bridge.calls, ['account/rateLimits/read'])


if __name__ == '__main__': unittest.main()
