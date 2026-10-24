"""公式APIの送信境界を検査する。実チャット・実serverは使用しない。"""
import unittest
from desktop_bridge import DesktopBridge, BridgeError

T = '123e4567-e89b-12d3-a456-426614174000'
A = '123e4567-e89b-12d3-a456-426614174001'


def thread(turn='failure', status='failed', state='idle', flags=None):
    return {'thread': {'id': T, 'status': {'type': state, 'activeFlags': flags or []},
                       'turns': [{'id': turn, 'status': status,
                                  'error': {'codexErrorInfo': 'usageLimitExceeded'} if status == 'failed' else None}]}}


class GuardedSend(unittest.TestCase):
    def setUp(self):
        self.bridge = DesktopBridge()
        self.bridge.capabilities = lambda: {'desktop_binding': True, 'guarded_admission': True,
                                             'safe_admission': False}
        self.calls = []
        self.current = thread()
        self.quota = {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
        self.response = {'turn': {'id': 'resumed'}}
        self.lost_reply = False
        self.live_turns = None
        self.persist_lag = False
        def request(method, params=None):
            self.calls.append((method, params))
            if method == 'account/rateLimits/read': return self.quota
            if method == 'thread/read': return self.current
            if method == 'thread/turns/list':
                self.assertEqual(params, {'threadId': T, 'sortDirection': 'desc', 'limit': 1})
                return {'data': self.live_turns if self.live_turns is not None else self.current['thread']['turns']}
            if method == 'turn/start':
                resumed = thread('resumed', 'inProgress', 'active')
                self.live_turns = resumed['thread']['turns']
                if not self.persist_lag:
                    self.current = resumed
                if self.lost_reply: raise TimeoutError()
                return self.response
            self.fail('未想定の操作')
        self.bridge.request = request

    def send(self, attempt=A):
        return self.bridge.send_once(T, 'failure', attempt_id=attempt)

    def writes(self):
        return [params for method, params in self.calls if method == 'turn/start']

    def test_sends_only_continue_with_durable_attempt_and_checks_receipt(self):
        self.assertEqual(self.send()['turn']['id'], 'resumed')
        self.assertEqual(self.writes(), [{'threadId': T, 'clientUserMessageId': A,
                                         'input': [{'type': 'text', 'text': '続けて'}]}])
        self.assertEqual(self.calls[-1][0], 'thread/turns/list')

    def test_changed_completed_active_or_approval_does_not_send(self):
        for value in (thread('manual', 'completed'), thread(state='active'),
                      thread(flags=['waitingOnApproval']), thread(flags=['waitingOnUserInput'])):
            with self.subTest(value=value):
                self.current = value
                with self.assertRaises(BridgeError): self.send()
                self.assertEqual(self.writes(), [])

    def test_either_quota_unavailable_does_not_send(self):
        for key in ('primary', 'secondary'):
            self.quota['rateLimits'][key]['usedPercent'] = 100
            with self.assertRaises(BridgeError): self.send()
            self.quota['rateLimits'][key]['usedPercent'] = 0
        self.assertEqual(self.writes(), [])

    def test_lost_reply_never_retries_same_failure_with_another_attempt(self):
        self.lost_reply = True
        with self.assertRaises(BridgeError): self.send()
        self.current = thread()
        with self.assertRaises(BridgeError):
            self.send('123e4567-e89b-12d3-a456-426614174002')
        self.assertEqual(len(self.writes()), 1)

    def test_wrong_or_missing_receipt_blocks_after_one_write(self):
        for result in ({}, {'turn': {'id': 'failure'}}, {'turn': {'id': 'unrelated'}}):
            with self.subTest(result=result):
                self.setUp()
                self.response = result
                with self.assertRaises(BridgeError): self.send()
                self.assertEqual(len(self.writes()), 1)

    def test_unconfigured_transport_never_advertises_sending(self):
        caps = DesktopBridge().capabilities()
        self.assertFalse(caps['safe_admission'])
        self.assertFalse(caps.get('guarded_admission', False))

    def test_arbitrary_command_is_rejected(self):
        with self.assertRaises(BridgeError):
            self.bridge.send_once(T, 'failure', command='別の指示', attempt_id=A)
        self.assertEqual(self.writes(), [])

    def test_observed_change_even_with_same_latest_turn_stops(self):
        self.bridge.snapshot(T)
        self.bridge.events.append({'method': 'turn/started', 'params': {'threadId': T}})
        with self.assertRaises(BridgeError): self.send()
        self.assertEqual(self.writes(), [])

    def test_prepare_only_loads_existing_thread(self):
        self.current['thread']['status']['type'] = 'notLoaded'
        original = self.bridge.request
        def request(method, params=None):
            if method == 'thread/resume':
                self.calls.append((method, params))
                return thread()
            return original(method, params)
        self.bridge.request = request
        self.bridge.prepare_thread(T)
        self.assertEqual([m for m, _ in self.calls], ['thread/read', 'thread/resume'])
        self.assertEqual(self.writes(), [])

    def test_live_receipt_succeeds_when_persisted_history_lags(self):
        self.persist_lag = True
        self.assertEqual(self.send()['turn']['id'], 'resumed')
        self.assertEqual(self.current['thread']['turns'][0]['id'], 'failure')
        self.assertEqual(self.bridge.snapshot(T)['latest_turn'], 'resumed')
        self.assertEqual(len(self.writes()), 1)

    def test_live_manual_turn_blocks_even_when_history_has_old_failure(self):
        self.live_turns = thread('manual', 'completed')['thread']['turns']
        with self.assertRaises(BridgeError): self.send()
        self.assertEqual(self.writes(), [])

    def test_missing_or_malformed_live_turn_never_falls_back_to_history(self):
        for turns in ([], [None], 'unknown'):
            with self.subTest(turns=turns):
                self.live_turns = turns
                with self.assertRaises(BridgeError): self.send()
                self.assertEqual(self.writes(), [])
