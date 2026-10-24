from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import queue_bridge

THREAD = '01a161ab-e82d-7631-a8aa-d33c77b2128e'
TURN = '01a161ab-e82d-7631-a8aa-d33c77b21281'
ATTEMPT = '01a161ab-e82d-7631-a8aa-d33c77b21282'
QUEUE = '01a161ab-e82d-7631-a8aa-d33c77b21283'


def live(**changes):
    result = {'id': THREAD, 'latest_turn': TURN, 'turn_status': 'failed', 'limited': True,
              'failure_turn': TURN, 'idle': True, 'awaiting': False, 'observation_verified': True}
    result.update(changes)
    return result


class QueueBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'codex.exe'
        self.config = {'mode': 'queue', 'codex_home': str(self.home), 'executable': str(self.path),
                       'sha256': 'hash', 'desktop_confirmed': True, 'manual_exclusion_acknowledged': True}
        self.evidence = {'home': str(self.home), 'executable': str(self.path), 'desktop': {'pid': 123}}
        self.transport = Mock()
        self.transport.check.return_value = True
        self.transport._verified = (self.path, 'hash')
        self.transport._owned = set()
        self.transport.enqueue.side_effect = self.enqueue
        self.ipc = Mock()
        self.ipc.__enter__ = Mock(return_value=self.ipc)
        self.ipc.__exit__ = Mock()
        self.ipc.snapshot.return_value = live()
        self.history = Mock()
        self.history.candidate.return_value = {'projection_current': True, 'latest_turn': TURN,
                                               'status': 'failed', 'failure': True}
        self.history.pending.return_value = []
        self.patches = [patch('queue_bridge.queue_platform.desktop_for_home', return_value=self.evidence),
                        patch('queue_bridge.queue_platform.revalidate', return_value=True),
                        patch('queue_bridge.queue_platform.loaded', return_value=True),
                        patch('queue_bridge.QueueTransport', return_value=self.transport),
                        patch('queue_bridge.DesktopIpc', return_value=self.ipc),
                        patch('queue_bridge.QueueHistory', return_value=self.history),
                        patch('queue_bridge._fingerprint', return_value=(self.path, 'hash')),
                        patch('queue_bridge.observe_resume_quota', return_value=(True, None))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def enqueue(self, thread, text, attempt, before_send=None):
        if before_send is not None and before_send() is not True:
            return {'outcome': 'not_started'}
        return {'outcome': 'accepted', 'queue_id': QUEUE}

    def bridge(self, **kwargs):
        return queue_bridge.QueueBridge(config=self.config, home=self.home, **kwargs)

    def test_context_native_ipc_and_config_guard(self):
        with self.bridge() as bridge:
            cap = bridge.capabilities()
            self.assertTrue(cap['desktop_binding'])
            self.assertTrue(cap['guarded_admission'])
            self.assertFalse(cap['safe_admission'])
        self.ipc.__exit__.assert_called_once()

    def test_prepare_config_confirms_only_connection_never_operator_consent(self):
        config = queue_bridge.prepare_config(self.home)
        self.assertEqual(config['mode'], 'queue')
        self.assertEqual(config['sha256'], 'hash')
        self.assertFalse(config['desktop_confirmed'])
        self.assertFalse(config['manual_exclusion_acknowledged'])
        self.transport.enqueue.assert_not_called()

    def test_readonly_pending_mode_needs_no_ack_but_cannot_send(self):
        self.config['manual_exclusion_acknowledged'] = False
        with self.bridge(for_pending=True) as bridge:
            self.assertTrue(bridge.capabilities()['desktop_binding'])
            self.assertEqual(bridge.snapshot(THREAD)['latest_turn'], TURN)
            with self.assertRaises(queue_bridge.BridgeError):
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_not_called()

    def test_live_snapshot_must_match_projection_and_native_binding(self):
        for candidate in ({'projection_current': False},
                          {'projection_current': True, 'latest_turn': ATTEMPT, 'status': 'failed', 'failure': True},
                          {'projection_current': True, 'latest_turn': TURN, 'status': 'completed', 'failure': False}):
            self.history.candidate.return_value = candidate
            with self.bridge() as bridge, self.assertRaises(queue_bridge.BridgeError):
                bridge.snapshot(THREAD)

    def test_prepare_thread_only_observes_loaded_state(self):
        with self.bridge() as bridge:
            bridge.prepare_thread(THREAD)
            self.transport.rpc.assert_not_called()
            with patch('queue_bridge.queue_platform.loaded', return_value=False):
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.prepare_thread(THREAD)

    def test_initial_admission_refusal_or_read_error_is_typed_definite_non_submission(self):
        for result, failure in ((False, None), (None, ValueError('private'))):
            with self.subTest(failure=failure), self.bridge() as bridge:
                with patch.object(bridge, '_admit', return_value=result, side_effect=failure):
                    with self.assertRaises(queue_bridge.QueueNotStarted):
                        bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_not_called()

    def test_duplicate_unknown_or_queued_attempt_is_never_typed_as_unsent(self):
        for response in ({'outcome': 'unknown'}, {'outcome': 'accepted', 'queue_id': QUEUE}):
            self.transport.enqueue.side_effect = None
            self.transport.enqueue.return_value = response
            with self.bridge() as bridge:
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
                with patch.object(bridge, '_admit', return_value=False) as admit:
                    with self.assertRaises(queue_bridge.BridgeError) as caught:
                        bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
                    self.assertNotIsInstance(caught.exception, queue_bridge.QueueNotStarted)
                    admit.assert_not_called()

    def test_invalid_or_pending_only_request_is_not_typed_as_fresh_unsent(self):
        for kwargs in ({'for_pending': True}, {}):
            with self.bridge(**kwargs) as bridge:
                with self.assertRaises(queue_bridge.BridgeError) as caught:
                    bridge.send_once(THREAD, TURN, attempt_id='invalid')
                self.assertNotIsInstance(caught.exception, queue_bridge.QueueNotStarted)
        with self.bridge(for_pending=True) as bridge:
            with self.assertRaises(queue_bridge.BridgeError) as caught:
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
            self.assertNotIsInstance(caught.exception, queue_bridge.QueueNotStarted)
        self.transport.enqueue.assert_not_called()

    def test_enqueue_exception_remains_unknown_after_admission(self):
        for error in (ValueError('private'), queue_bridge.QueueNotStarted('fixture')):
            self.transport.enqueue.reset_mock()
            self.transport.enqueue.side_effect = error
            with self.bridge() as bridge:
                self.assertEqual(bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT), {'delivery': 'unknown'})
            self.transport.enqueue.assert_called_once()

    def test_send_returns_queue_receipt_and_blocks_duplicate_failure(self):
        with self.bridge() as bridge:
            self.assertEqual(bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT),
                             {'delivery': 'queued', 'queue': {'id': QUEUE}})
            with self.assertRaises(queue_bridge.BridgeError):
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_called_once()
        self.assertEqual(self.transport.enqueue.call_args.args, (THREAD, '続けて', ATTEMPT))

    def test_unknown_is_not_a_turn_and_is_not_retried(self):
        self.transport.enqueue.side_effect = None
        self.transport.enqueue.return_value = {'outcome': 'unknown'}
        with self.bridge() as bridge:
            self.assertEqual(bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT), {'delivery': 'unknown'})
            with self.assertRaises(queue_bridge.BridgeError):
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_called_once()

    def test_completed_pending_or_waiting_never_sends(self):
        for value in (live(limited=False, turn_status='completed'), live(awaiting=True), live(idle=False)):
            self.ipc.snapshot.return_value = value
            self.history.candidate.return_value = {'projection_current': True, 'latest_turn': TURN,
                                                   'status': value['turn_status'], 'failure': value['limited']}
            with self.bridge() as bridge, self.assertRaises(queue_bridge.BridgeError):
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_not_called()

    def test_operator_refusal_or_manual_change_during_final_guard_prevents_queue(self):
        with self.bridge() as bridge:
            result = bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT, before_send=lambda: False)
            self.assertEqual(result['delivery'], 'not_started')
        with self.bridge() as bridge:
            original = self.ipc.snapshot.return_value
            self.ipc.snapshot.side_effect = [original, live(latest_turn=ATTEMPT)]
            result = bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
            self.assertEqual(result['delivery'], 'not_started')

    def test_invalid_selected_identity_or_consent_blocks_send(self):
        for key, value in (('sha256', 'changed'), ('executable', '/other/codex'),
                           ('desktop_confirmed', False), ('manual_exclusion_acknowledged', False)):
            config = dict(self.config, **{key: value})
            with queue_bridge.QueueBridge(config=config, home=self.home) as bridge:
                self.assertFalse(bridge.capabilities()['guarded_admission'])
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_not_called()

    def test_cancel_only_exact_own_client_receipt_plain_continuation(self):
        self.history.pending.return_value = [{'queue_id': QUEUE, 'client_id': ATTEMPT,
                                              'input': [{'type': 'text', 'text': '続けて'}]}]
        self.transport.rpc.return_value = {'deleted': True}
        with self.bridge(for_pending=True) as bridge:
            result = bridge.cancel_pending(THREAD, ATTEMPT, QUEUE)
        self.assertEqual(result, {'delivery': 'unknown', 'deleted': True})
        self.assertIn((THREAD, QUEUE), self.transport._owned)
        self.assertEqual(self.transport.rpc.call_args.args,
                         ('thread/queue/delete', {'threadId': THREAD, 'queuedSubmissionId': QUEUE}))

    def test_pending_cleanup_can_verify_saved_cli_without_desktop_but_never_send(self):
        self.history.pending.return_value = [{'queue_id': QUEUE, 'client_id': ATTEMPT,
                                              'input': [{'type': 'text', 'text': '続けて'}]}]
        self.transport.rpc.return_value = {'deleted': True}
        with patch('queue_bridge.queue_platform.desktop_for_home', side_effect=ValueError('absent')):
            with self.bridge(for_pending=True) as bridge:
                self.assertFalse(bridge.capabilities()['desktop_binding'])
                self.assertFalse(bridge.capabilities()['guarded_admission'])
                self.assertEqual(bridge.find_pending(THREAD, ATTEMPT), QUEUE)
                self.assertTrue(bridge.cancel_pending(THREAD, ATTEMPT, QUEUE)['deleted'])
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.snapshot(THREAD)
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_not_called()

    def test_offline_cleanup_rejects_relative_home_and_modified_cli(self):
        for key, value in (('codex_home', '.'), ('sha256', 'changed')):
            with patch('queue_bridge.queue_platform.desktop_for_home', side_effect=ValueError('absent')):
                with self.assertRaises(queue_bridge.BridgeError):
                    with queue_bridge.QueueBridge(config=dict(self.config, **{key: value}),
                                                  home=self.home, for_pending=True):
                        pass

    def test_find_pending_requires_single_exact_attempt_and_plain_input(self):
        row = {'queue_id': QUEUE, 'client_id': ATTEMPT, 'input': [{'type': 'text', 'text': '続けて'}]}
        with self.bridge(for_pending=True) as bridge:
            self.assertIsNone(bridge.find_pending(THREAD, ATTEMPT))
            self.history.pending.return_value = [row, row]
            with self.assertRaises(queue_bridge.BridgeError):
                bridge.find_pending(THREAD, ATTEMPT)
            self.history.pending.side_effect = ValueError('private')
            with self.assertRaises(queue_bridge.BridgeError):
                bridge.find_pending(THREAD, ATTEMPT)

    def test_receipt_passes_exact_attempt_as_client_id_without_claiming_queue_is_turn(self):
        self.history.receipt.return_value = {'delivery': 'pending'}
        with self.bridge(for_pending=True) as bridge:
            self.assertEqual(bridge.receipt(THREAD, ATTEMPT, QUEUE), {'delivery': 'pending'})
        self.history.receipt.assert_called_once_with(THREAD, ATTEMPT, queue_id=QUEUE, client_id=ATTEMPT)

    def test_queue_already_pending_or_quota_not_recovered_blocks_send(self):
        self.history.pending.return_value = [{'queue_id': QUEUE}]
        with self.bridge() as bridge, self.assertRaises(queue_bridge.BridgeError):
            bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.history.pending.return_value = []
        with patch('queue_bridge.observe_resume_quota', return_value=(False, None)):
            with self.bridge() as bridge, self.assertRaises(queue_bridge.BridgeError):
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.transport.enqueue.assert_not_called()

    def test_official_serialized_plain_text_can_be_cancelled(self):
        self.history.pending.return_value = [{'queue_id': QUEUE, 'client_id': ATTEMPT,
                                              'input': [{'type': 'text', 'text': '続けて', 'text_elements': []}]}]
        self.transport.rpc.return_value = {'deleted': True}
        with self.bridge() as bridge:
            self.assertEqual(bridge.find_pending(THREAD, ATTEMPT), QUEUE)
            self.assertTrue(bridge.cancel_pending(THREAD, ATTEMPT, QUEUE)['deleted'])

    def test_rich_or_unknown_text_fields_cannot_be_cancelled(self):
        for extra in ({'text_elements': [{}]}, {'text_elements': None}, {'foreign': True}):
            with self.subTest(extra=extra):
                self.history.pending.return_value = [{'queue_id': QUEUE, 'client_id': ATTEMPT,
                                                      'input': [dict(type='text', text='続けて', **extra)]}]
                with self.bridge() as bridge:
                    self.assertIsNone(bridge.find_pending(THREAD, ATTEMPT))
                    self.assertFalse(bridge.cancel_pending(THREAD, ATTEMPT, QUEUE)['deleted'])

    def test_foreign_queue_cancel_is_refused(self):
        for row in ({'queue_id': QUEUE, 'client_id': TURN, 'input': [{'type': 'text', 'text': '続けて'}]},
                    {'queue_id': QUEUE, 'client_id': ATTEMPT, 'input': [{'type': 'text', 'text': 'other'}]}):
            self.history.pending.return_value = [row]
            with self.bridge(for_pending=True) as bridge:
                self.assertEqual(bridge.cancel_pending(THREAD, ATTEMPT, QUEUE)['deleted'], False)
        self.transport.rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
