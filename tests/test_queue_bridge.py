from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import desktop_ipc
import queue_bridge
from tests.test_desktop_ipc import CLIENT, OWNER, Stream, state

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

    def reader(self):
        reader = Mock()
        reader.__enter__ = Mock(return_value=reader)
        reader.__exit__ = Mock()
        reader.snapshot.return_value = live()
        return reader

    def test_snapshot_discards_reader_with_queued_patch_before_next_observation(self):
        first, second = self.reader(), self.reader()
        first.snapshot.side_effect = [live(), ValueError('unknown_change')]
        with patch('queue_bridge.DesktopIpc', side_effect=[first, second]) as factory:
            with self.bridge() as bridge:
                self.assertEqual(bridge.snapshot(THREAD), live())
                self.assertTrue(bridge.connected)
                self.assertIs(bridge.evidence, self.evidence)
                self.assertTrue(bridge.capabilities()['guarded_admission'])
                self.assertEqual(bridge.snapshot(THREAD), live())
                first.__exit__.assert_called_once()
                self.assertIsNone(bridge.ipc)
                second.__exit__.assert_called_once()
                self.assertEqual(factory.call_count, 2)
                for invocation in factory.call_args_list:
                    self.assertIs(invocation.args[0], self.evidence)
                    self.assertEqual(invocation.kwargs, {'timeout': 15})
        first.snapshot.assert_called_once_with(THREAD)
        second.snapshot.assert_called_once_with(THREAD)
        first.__enter__.assert_called_once()
        second.__enter__.assert_called_once()
        first.__exit__.assert_called_once()
        second.__exit__.assert_called_once()
        self.transport.check.assert_called_once()

    def test_snapshot_exception_closes_reader_and_next_snapshot_uses_fresh_reader(self):
        first, second = self.reader(), self.reader()
        first.snapshot.side_effect = ValueError('unknown_change')
        with patch('queue_bridge.DesktopIpc', side_effect=[first, second]):
            with self.bridge() as bridge:
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.snapshot(THREAD)
                first.__exit__.assert_called_once()
                self.assertEqual(bridge.snapshot(THREAD), live())
        first.__exit__.assert_called_once()
        second.__exit__.assert_called_once()

    def buffered_patch_stream(self):
        stream = Stream(state(turns=[{'turnId': TURN, 'status': 'failed',
                                     'error': {'codexErrorInfo': 'usageLimitExceeded'}}]))
        write = stream.write

        def enqueue_patch(payload, deadline):
            write(payload, deadline)
            if stream.sent[-1]['method'] == 'thread-follower-load-complete-history':
                stream.push({'type': 'broadcast', 'method': 'thread-stream-state-changed',
                             'sourceClientId': OWNER, 'version': 11, 'targetClientIds': [CLIENT],
                             'params': {'conversationId': THREAD, 'hostId': 'local',
                                        'change': {'type': 'patch', 'revision': 7, 'patches': []}}})

        stream.write = enqueue_patch
        return stream

    def test_buffered_patch_fixture_fails_closed_when_native_reader_is_reused(self):
        stream = self.buffered_patch_stream()
        with patch('desktop_ipc._connect', return_value=stream):
            with desktop_ipc.DesktopIpc(self.evidence) as reader:
                self.assertEqual(reader.snapshot(THREAD), live())
                with self.assertRaises(desktop_ipc.DesktopIpcError):
                    reader.snapshot(THREAD)
        self.assertTrue(stream.closed)

    def test_snapshot_uses_fresh_native_reader_despite_buffered_patch(self):
        first, second = self.buffered_patch_stream(), self.buffered_patch_stream()
        with patch('queue_bridge.DesktopIpc', desktop_ipc.DesktopIpc), \
                patch('desktop_ipc._connect', side_effect=[first, second]) as connect:
            with self.bridge() as bridge:
                self.assertEqual(bridge.snapshot(THREAD), live())
                self.assertEqual(bridge.snapshot(THREAD), live())
                self.assertEqual(connect.call_count, 2)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    def test_fresh_reader_enter_failure_is_closed_and_next_reader_is_fresh(self):
        first, broken, third = self.reader(), self.reader(), self.reader()
        broken.__enter__.side_effect = ValueError('origin')
        with patch('queue_bridge.DesktopIpc', side_effect=[first, broken, third]):
            with self.bridge() as bridge:
                bridge.snapshot(THREAD)
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.snapshot(THREAD)
                broken.__exit__.assert_called_once()
                broken.snapshot.assert_not_called()
                self.assertEqual(bridge.snapshot(THREAD), live())
        for reader in (first, broken, third):
            reader.__exit__.assert_called_once()

    def test_initial_reader_enter_failure_is_closed_and_never_reads(self):
        self.ipc.__enter__.side_effect = ValueError('owner')
        bridge = self.bridge()
        with self.assertRaises(queue_bridge.BridgeError):
            bridge.__enter__()
        bridge._close()
        self.ipc.__exit__.assert_called_once()
        self.ipc.snapshot.assert_not_called()
        self.assertFalse(bridge.connected)
        self.assertIsNone(bridge.evidence)

    def test_snapshot_close_failure_is_detached_and_bridge_close_is_idempotent(self):
        first, second = self.reader(), self.reader()
        first.__exit__.side_effect = ValueError('close')
        with patch('queue_bridge.DesktopIpc', side_effect=[first, second]) as factory:
            with self.bridge() as bridge:
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.snapshot(THREAD)
                self.assertIsNone(bridge.ipc)
                self.assertFalse(bridge.connected)
                self.assertFalse(bridge.capabilities()['desktop_binding'])
                self.assertFalse(bridge.capabilities()['guarded_admission'])
                with self.assertRaises(queue_bridge.BridgeError):
                    bridge.snapshot(THREAD)
                with self.assertRaises(queue_bridge.QueueNotStarted):
                    bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
                bridge._close()
                bridge._close()
            self.assertIsNone(bridge.ipc)
        factory.assert_called_once()
        first.__exit__.assert_called_once()
        second.__enter__.assert_not_called()
        self.transport.enqueue.assert_not_called()

    def test_snapshot_read_and_close_failure_disable_admission(self):
        self.ipc.snapshot.side_effect = ValueError('snapshot')
        self.ipc.__exit__.side_effect = ValueError('close')
        with self.bridge() as bridge:
            with self.assertRaises(queue_bridge.BridgeError):
                bridge.snapshot(THREAD)
            self.assertFalse(bridge.connected)
            self.assertFalse(bridge.capabilities()['guarded_admission'])
            with self.assertRaises(queue_bridge.QueueNotStarted):
                bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.ipc.__exit__.assert_called_once()
        self.transport.enqueue.assert_not_called()

    def test_initial_reader_enter_and_close_failure_leave_bridge_disabled(self):
        self.ipc.__enter__.side_effect = ValueError('owner')
        self.ipc.__exit__.side_effect = ValueError('close')
        bridge = self.bridge()
        with self.assertRaises(queue_bridge.BridgeError):
            bridge.__enter__()
        bridge._close()
        self.assertIsNone(bridge.ipc)
        self.assertFalse(bridge.capabilities()['guarded_admission'])
        with self.assertRaises(queue_bridge.QueueNotStarted):
            bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.ipc.__exit__.assert_called_once()
        self.transport.enqueue.assert_not_called()

    def test_context_close_failure_disables_bridge_and_is_idempotent(self):
        self.ipc.__exit__.side_effect = ValueError('close')
        bridge = self.bridge()
        bridge.__enter__()
        bridge.__exit__(None, None, None)
        bridge.__exit__(None, None, None)
        self.assertIsNone(bridge.ipc)
        self.assertFalse(bridge.capabilities()['guarded_admission'])
        with self.assertRaises(queue_bridge.BridgeError):
            bridge.snapshot(THREAD)
        with self.assertRaises(queue_bridge.QueueNotStarted):
            bridge.send_once(THREAD, TURN, attempt_id=ATTEMPT)
        self.ipc.__exit__.assert_called_once()
        self.transport.enqueue.assert_not_called()

    def test_snapshot_closes_reader_on_loaded_history_and_binding_rejection(self):
        for target in ('loaded', 'history', 'revalidate'):
            with self.subTest(target=target):
                reader = self.reader()
                with patch('queue_bridge.DesktopIpc', return_value=reader):
                    with self.bridge() as bridge:
                        if target == 'history':
                            self.history.candidate.side_effect = ValueError('history')
                        with patch('queue_bridge.queue_platform.loaded', return_value=target != 'loaded'), \
                                patch('queue_bridge.queue_platform.revalidate', return_value=target != 'revalidate'):
                            with self.assertRaises(queue_bridge.BridgeError):
                                bridge.snapshot(THREAD)
                        reader.__exit__.assert_called_once()
                self.history.candidate.side_effect = None

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
