import pathlib
import tempfile
import unittest
import uuid
from unittest import mock

import auto_watch
import core
import queue_state
import watch


NOW = 1800000000


class QueueWatchBridge:
    def __init__(self, current):
        self.current = dict(current)
        self.queue = str(uuid.uuid4())
        self.response = {'delivery': 'queued', 'queue': {'id': self.queue}}
        self.delivery = {'delivery': 'pending'}
        self.send_calls = []
        self.actual_sends = []
        self.cancel_calls = []
        self.receipt_calls = []
        self.before_send_hook = None
        self.after_send_hook = None
        self.on_receipt = None
        self.on_cancel = None
        self.on_snapshot = None
        self.found = None
        self.foreign_queue = False
        self.contexts = 0
        self.snapshots = 0

    def __enter__(self):
        self.contexts += 1
        return self

    def __exit__(self, *args):
        return None

    def capabilities(self):
        return {'desktop_binding': True, 'safe_admission': False, 'guarded_admission': True}

    def prepare_thread(self, thread):
        if thread != self.current['id']:
            raise ValueError('unexpected thread')

    def snapshot(self, thread):
        self.snapshots += 1
        if self.on_snapshot:
            self.on_snapshot(self.snapshots)
        return dict(self.current)

    def quota(self):
        return {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}

    def send_once(self, thread, failure, *, attempt_id=None, before_send=None):
        self.send_calls.append((thread, failure, attempt_id))
        if self.before_send_hook:
            self.before_send_hook()
        if self.foreign_queue or (before_send is not None and before_send() is not True):
            return {'delivery': 'not_started'}
        self.actual_sends.append((thread, failure, attempt_id))
        if self.after_send_hook:
            self.after_send_hook()
        return dict(self.response)

    def receipt(self, thread, attempt, queue_id=None):
        self.receipt_calls.append((thread, attempt, queue_id))
        if self.on_receipt:
            self.on_receipt()
        return dict(self.delivery)

    def find_pending(self, thread, attempt):
        return self.found

    def cancel_pending(self, thread, attempt, queue):
        self.cancel_calls.append((thread, attempt, queue))
        if self.on_cancel:
            self.on_cancel()
        return {'delivery': 'unknown', 'deleted': True}


class QueueWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name) / 'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        self.thread = str(uuid.uuid4())
        self.failure = str(uuid.uuid4())
        self.turn = str(uuid.uuid4())
        self.current = {'id': self.thread, 'title': '試験会話', 'cwd': '', 'limited': True,
                        'failure_turn': self.failure, 'latest_turn': self.failure,
                        'turn_status': 'failed', 'observation_verified': True,
                        'awaiting': False, 'idle': True,
                        'error': {'codexErrorInfo': 'usageLimitExceeded'}}
        self.local = dict(self.current, observation_verified=False)
        self.bridge = QueueWatchBridge(self.current)
        self.now = NOW
        self.mode = 'queue'
        auto_watch.initialize(self.db)
        auto_watch.set_enabled(self.db, True)

    def tick(self):
        with mock.patch('watch.connection_options', return_value={'mode': self.mode}), mock.patch('watch.discover', return_value=([dict(self.local)], None)), mock.patch('watch.time.time', side_effect=lambda: self.now):
            return watch.once(self.db, lambda: self.bridge)

    def thread_row(self):
        return core.list_threads(self.db)[0]

    def started(self):
        result = self.tick()
        self.assertIn((self.thread, 'queued'), result.get('results', []))
        self.assertEqual(len(self.bridge.actual_sends), 1)
        return queue_state.pending(self.db)[0]

    def complete_receipt(self, status='inProgress'):
        self.bridge.delivery = {'delivery': 'reached', 'turn': {'id': self.turn}}
        self.bridge.current.update(latest_turn=self.turn, failure_turn=None, limited=False,
                                   turn_status=status, error=None)
        self.local.update(self.bridge.current, observation_verified=False)

    def test_acceptance_id_is_not_a_turn_receipt(self):
        self.started()
        self.assertIsNone(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0])
        self.assertEqual(self.thread_row()['state'], 'claimed')

    def test_definite_non_submission_keeps_history_without_blocking_shutdown(self):
        self.bridge.foreign_queue = True
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(queue_state.pending(self.db), [])
        self.assertEqual(self.db.execute('SELECT state FROM queue_dispatches').fetchone()[0], 'cancelled')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(len(self.bridge.send_calls), 1)
        self.assertIsNone(self.thread_row()['resume_turn'])
        self.assertNotEqual(self.bridge.queue, self.turn)

    def test_initial_bridge_admission_rejection_leaves_no_pending_and_keeps_latch(self):
        import app
        import queue_bridge
        for failure in (False, ValueError('fixture')):
            with self.subTest(failure=failure):
                real = queue_bridge.QueueBridge.__new__(queue_bridge.QueueBridge)
                real.for_pending = False
                real.attempted_failures = set()
                real.transport = mock.Mock()
                real._admit = mock.Mock(return_value=False,
                    side_effect=failure if isinstance(failure, Exception) else None)
                self.bridge.send_once = mock.Mock(wraps=real.send_once)
                self.tick()
                real.transport.enqueue.assert_not_called()
                self.assertEqual(queue_state.pending(self.db), [])
                self.assertEqual(self.db.execute('SELECT state FROM queue_dispatches').fetchone()[0], 'cancelled')
                self.assertEqual(self.db.execute('SELECT state FROM attempts').fetchone()[0], 'cancelled')
                self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
                self.tick()
                self.bridge.send_once.assert_called_once()
                self.assertTrue(app.reconcile_pending_shutdown(self.home))
                self.db.execute('DELETE FROM queue_dispatches')
                self.db.execute('DELETE FROM attempts')
                self.db.execute('DELETE FROM threads')
                auto_watch.set_enabled(self.db, True)

    def test_generic_send_failure_never_clears_pending_as_definite_non_submission(self):
        from desktop_bridge import BridgeError
        self.bridge.send_once = mock.Mock(side_effect=BridgeError('fixture'))
        self.tick()
        self.assertEqual(len(queue_state.pending(self.db)), 1)
        self.assertNotEqual(self.db.execute('SELECT state FROM queue_dispatches').fetchone()[0], 'cancelled')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)
        self.tick()
        self.bridge.send_once.assert_called_once()

    def test_reached_on_next_tick_records_actual_turn_and_monitors(self):
        self.started()
        self.complete_receipt()
        self.tick()
        self.assertEqual(self.thread_row()['state'], 'monitoring')
        self.assertEqual(self.thread_row()['resume_turn'], self.turn)
        self.assertEqual(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0], self.turn)
        self.assertEqual(len(self.bridge.actual_sends), 1)

    def test_completed_actual_receipt_finishes_without_new_send(self):
        self.started()
        self.complete_receipt('completed')
        self.tick()
        self.assertEqual(self.thread_row()['state'], 'completed')
        self.assertFalse(self.thread_row()['enabled'])
        self.assertEqual(len(self.bridge.actual_sends), 1)

    def test_pending_claim_is_protected_from_legacy_unknown_block(self):
        self.started()
        self.now += 1
        self.tick()
        self.assertEqual(self.thread_row()['state'], 'claimed')
        self.assertTrue(self.thread_row()['enabled'])
        self.assertEqual(queue_state.pending(self.db)[0]['state'], 'queued')
        self.assertEqual(len(self.bridge.send_calls), 1)
        self.assertEqual(self.bridge.cancel_calls, [])

    def test_off_reconciles_and_cancels_without_new_send(self):
        row = self.started()
        auto_watch.set_enabled(self.db, False)
        result = self.tick()
        self.assertEqual(result['state'], 'disabled')
        self.assertEqual(self.bridge.cancel_calls, [(self.thread, row['attempt_id'], self.bridge.queue)])
        self.assertEqual(len(self.bridge.actual_sends), 1)
        self.assertEqual(queue_state.pending(self.db)[0]['state'], 'unknown')

    def test_deleted_queue_is_not_proof_of_non_delivery(self):
        self.started()
        auto_watch.set_enabled(self.db, False)
        self.bridge.on_cancel = lambda: setattr(self.bridge, 'delivery', {'delivery': 'unknown'})
        self.tick()
        self.assertEqual(queue_state.pending(self.db)[0]['state'], 'unknown')
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertIsNone(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0])

    def test_receipt_winning_cancel_race_is_recorded_and_keeps_off(self):
        self.started()
        auto_watch.set_enabled(self.db, False)
        self.bridge.on_cancel = lambda: setattr(self.bridge, 'delivery', {'delivery': 'reached', 'turn': {'id': self.turn}})
        self.tick()
        self.assertEqual(queue_state.pending(self.db), [])
        self.assertEqual(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0], self.turn)
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertFalse(self.thread_row()['enabled'])

    def test_uncertain_response_learns_own_queue_then_cancels_never_resends(self):
        self.bridge.response = {'delivery': 'unknown'}
        self.tick()
        self.assertEqual(len(self.bridge.actual_sends), 1)
        self.assertEqual(queue_state.pending(self.db)[0]['state'], 'unknown')
        self.bridge.found = self.bridge.queue
        self.tick()
        current = queue_state.pending(self.db)[0]
        self.assertEqual(current['queue_id'], self.bridge.queue)
        self.assertEqual(current['state'], 'unknown')
        self.assertEqual(len(self.bridge.cancel_calls), 1)
        self.assertEqual(len(self.bridge.actual_sends), 1)

    def test_policy_change_at_before_send_blocks_submission(self):
        self.bridge.before_send_hook = lambda: auto_watch.set_enabled(self.db, False)
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_thread_generation_change_at_before_send_blocks_submission(self):
        self.bridge.before_send_hook = lambda: core.enable(self.db, self.thread, False)
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_network_submission_and_receipt_do_not_hold_sqlite_writer_lock(self):
        writable = []

        def probe():
            second = core.connection(self.home)
            try:
                second.execute('BEGIN IMMEDIATE')
                second.execute('ROLLBACK')
                writable.append(True)
            finally:
                second.close()

        self.bridge.before_send_hook = probe
        self.started()
        self.bridge.on_receipt = probe
        self.tick()
        self.assertGreaterEqual(len(writable), 2)
        self.assertTrue(all(writable))

    def test_changed_failure_before_send_never_submits(self):
        def changed(count):
            if count >= 2:
                self.bridge.current.update(latest_turn=str(uuid.uuid4()), failure_turn=str(uuid.uuid4()))

        self.bridge.on_snapshot = changed
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertEqual(self.bridge.send_calls, [])
        self.assertEqual(queue_state.pending(self.db), [])
        self.assertEqual(self.db.execute('SELECT state FROM queue_dispatches').fetchone()[0], 'cancelled')
        self.assertEqual(self.db.execute('SELECT state FROM attempts').fetchone()[0], 'cancelled')

    def test_approval_pending_is_never_submitted(self):
        self.bridge.current['awaiting'] = True
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])

    def test_already_completed_is_never_submitted(self):
        self.bridge.current.update(turn_status='completed', limited=False, failure_turn=None)
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])

    def test_foreign_queue_admission_failure_does_not_retry(self):
        self.bridge.foreign_queue = True
        self.tick()
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(len(self.bridge.send_calls), 1)
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_timeout_blocks_and_retains_unknown_for_reconciliation(self):
        self.started()
        self.now += 31
        self.tick()
        self.assertEqual(len(self.bridge.cancel_calls), 1)
        self.assertEqual(queue_state.pending(self.db)[0]['state'], 'unknown')
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertEqual(len(self.bridge.actual_sends), 1)

    def test_queue_id_presented_as_turn_is_not_accepted(self):
        self.started()
        self.bridge.delivery = {'delivery': 'reached', 'turn': {'id': self.bridge.queue}}
        self.tick()
        self.assertEqual(queue_state.pending(self.db)[0]['state'], 'unknown')
        self.assertIsNone(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0])
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_legacy_mode_without_queue_table_keeps_original_receipt_path(self):
        self.mode = 'socket'

        def legacy_send(thread, failure, *, attempt_id=None):
            self.bridge.actual_sends.append((thread, failure, attempt_id))
            return {'turn': {'id': self.turn}}

        self.bridge.send_once = legacy_send
        result = self.tick()
        self.assertIn((self.thread, 'dispatched'), result.get('results', []))
        self.assertEqual(self.thread_row()['resume_turn'], self.turn)
        self.assertEqual(self.thread_row()['state'], 'monitoring')
        self.assertEqual(queue_state.pending(self.db), [])
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='queue_dispatches'").fetchone())

    def test_reopened_database_reconciles_accepted_queue_without_resend(self):
        self.started()
        self.db.close()
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        self.now += 1
        self.tick()
        self.assertEqual(self.thread_row()['state'], 'claimed')
        self.assertEqual(len(self.bridge.actual_sends), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_off_after_submission_keeps_acceptance_for_next_tick_cancellation(self):
        self.bridge.after_send_hook = lambda: auto_watch.set_enabled(self.db, False)
        row = self.started()
        self.assertEqual(row['queue_id'], self.bridge.queue)
        self.assertFalse(self.thread_row()['enabled'])
        self.tick()
        self.assertEqual(len(self.bridge.cancel_calls), 1)
        self.assertEqual(len(self.bridge.actual_sends), 1)

    def test_actual_receipt_is_recorded_once_and_approval_stops_followup(self):
        with mock.patch('core.record_receipt', wraps=core.record_receipt) as receipt:
            self.started()
            self.assertEqual(receipt.call_count, 0)
            self.complete_receipt()
            self.tick()
            self.assertEqual(receipt.call_count, 1)
            self.bridge.current['awaiting'] = True
            self.local.update(self.bridge.current, observation_verified=False)
            self.tick()
            self.assertEqual(receipt.call_count, 1)
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertEqual(len(self.bridge.actual_sends), 1)

    def test_new_quota_failure_of_received_turn_can_be_registered_once(self):
        self.started()
        self.complete_receipt()
        self.tick()
        self.bridge.current.update(turn_status='failed', limited=True, failure_turn=self.turn,
                                   error={'codexErrorInfo': 'usageLimitExceeded'})
        self.local.update(self.bridge.current, observation_verified=False)
        self.bridge.queue = str(uuid.uuid4())
        self.bridge.response = {'delivery': 'queued', 'queue': {'id': self.bridge.queue}}
        self.bridge.delivery = {'delivery': 'pending'}
        self.tick()
        self.assertEqual(len(self.bridge.actual_sends), 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 2)
        self.assertEqual(queue_state.pending(self.db)[0]['failure_turn'], self.turn)
        self.tick()
        self.assertEqual(len(self.bridge.actual_sends), 2)

    def test_quota_word_in_message_is_not_structured_quota_failure(self):
        self.bridge.current.update(limited=False, failure_turn=None,
                                   error={'message': 'quoted usageLimitExceeded'})
        self.local.update(self.bridge.current, observation_verified=False)
        self.tick()
        self.assertEqual(self.bridge.actual_sends, [])
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
