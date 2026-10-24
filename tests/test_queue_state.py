import pathlib
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

import auto_watch
import core
import queue_state as state


class QueueStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = core.connection(pathlib.Path(self.temp.name) / 'state')
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.thread = str(uuid.uuid4())
        self.failure = str(uuid.uuid4())
        self.queue = str(uuid.uuid4())
        self.turn = str(uuid.uuid4())
        self.db.execute("INSERT INTO threads (id,title,enabled,state,failure_turn,generation) VALUES (?,'試験',1,'claimed',?,3)", (self.thread, self.failure))
        self.db.execute('INSERT INTO auto_watch_policy VALUES (1,1,4)')
        self.intent = core.record_intent(self.db, self.thread)

    def begin(self):
        state.initialise(self.db)
        return state.start(self.db, self.thread, self.intent, now=1800000000)

    def thread_row(self):
        return dict(self.db.execute('SELECT * FROM threads WHERE id=?', (self.thread,)).fetchone())

    def test_opt_in_schema_preserves_core_version_and_columns(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        columns = list(self.db.execute('PRAGMA table_info(attempts)'))
        self.assertEqual(state.pending(self.db), [])
        state.initialise(self.db)
        state.initialise(self.db)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], version)
        self.assertEqual(list(self.db.execute('PRAGMA table_info(attempts)')), columns)

    def test_intent_is_required_and_first_start_is_unique(self):
        row = self.begin()
        self.assertEqual(row['attempt_id'], self.intent[2])
        self.assertTrue(state.permitted(self.db, row))
        self.assertIsNone(state.start(self.db, self.thread, self.intent))
        with self.assertRaises(ValueError):
            state.start(self.db, self.thread, (self.failure, 3, str(uuid.uuid4()), 4))

    def test_accepted_is_idempotent_but_id_is_immutable(self):
        row = self.begin()
        state.accepted(self.db, row, self.queue)
        state.accepted(self.db, row, self.queue)
        current = state.pending(self.db)[0]
        self.assertEqual(current['state'], 'queued')
        self.assertEqual(current['queue_id'], self.queue)
        self.assertFalse(state.permitted(self.db, row))
        with self.assertRaises(ValueError):
            state.accepted(self.db, row, str(uuid.uuid4()))

    def test_unknown_is_durable_and_never_resends(self):
        row = self.begin()
        state.uncertain(self.db, row)
        self.assertEqual(state.pending(self.db)[0]['state'], 'unknown')
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertFalse(self.thread_row()['enabled'])
        self.assertFalse(state.permitted(self.db, row))
        self.assertIsNone(state.start(self.db, self.thread, self.intent))

    def test_reached_requires_actual_turn_and_preserves_receipt(self):
        row = self.begin()
        state.accepted(self.db, row, self.queue)
        with self.assertRaises(ValueError):
            state.reached(self.db, row, self.queue)
        state.reached(self.db, row, self.turn)
        state.reached(self.db, row, self.turn)
        self.assertEqual(state.pending(self.db), [])
        self.assertEqual(self.thread_row()['resume_turn'], self.turn)
        self.assertEqual(self.thread_row()['state'], 'monitoring')
        with self.assertRaises(ValueError):
            state.reached(self.db, row, str(uuid.uuid4()))

    def test_policy_off_before_send_denied_and_late_receipt_stays_blocked(self):
        row = self.begin()
        auto_watch.set_enabled(self.db, False)
        self.assertFalse(state.permitted(self.db, row))
        state.accepted(self.db, row, self.queue)
        state.reached(self.db, row, self.turn)
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertFalse(self.thread_row()['enabled'])
        self.assertEqual(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0], self.turn)

    def test_policy_generation_change_without_thread_generation_is_denied(self):
        row = self.begin()
        self.db.execute('UPDATE auto_watch_policy SET generation=generation+1')
        self.assertFalse(state.permitted(self.db, row))
        state.reached(self.db, row, self.turn)
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_foreign_or_modified_dispatch_row_is_rejected(self):
        row = self.begin()
        for field, value in [('thread_id', str(uuid.uuid4())), ('failure_turn', str(uuid.uuid4())), ('generation', 999), ('policy_generation', 999), ('attempt_id', str(uuid.uuid4())), ('queue_id', str(uuid.uuid4()))]:
            changed = dict(row, **{field: value})
            self.assertFalse(state.permitted(self.db, changed))
            with self.assertRaises(ValueError):
                state.accepted(self.db, changed, self.queue)

    def test_cancelled_attempt_is_terminal_and_latched(self):
        row = self.begin()
        state.accepted(self.db, row, self.queue)
        state.cancelled(self.db, row)
        state.cancelled(self.db, row)
        self.assertEqual(state.pending(self.db), [])
        self.assertEqual(self.db.execute('SELECT state FROM attempts').fetchone()[0], 'cancelled')
        self.assertFalse(self.thread_row()['enabled'])
        self.assertFalse(state.permitted(self.db, row))

    def test_receipt_exception_rolls_back_all_own_changes(self):
        row = self.begin()
        with mock.patch('core.record_receipt', side_effect=ValueError('receipt unavailable')):
            with self.assertRaises(ValueError):
                state.reached(self.db, row, self.turn)
        self.assertEqual(state.pending(self.db)[0]['state'], 'sending')
        self.assertEqual(self.thread_row()['state'], 'claimed')

    def test_writes_participate_in_outer_transaction_without_committing_it(self):
        row = self.begin()
        self.db.execute('BEGIN IMMEDIATE')
        state.accepted(self.db, row, self.queue)
        self.assertTrue(self.db.in_transaction)
        self.db.execute('ROLLBACK')
        self.assertIsNone(state.pending(self.db)[0]['queue_id'])

    def test_invalid_time_or_queue_id_rejected(self):
        state.initialise(self.db)
        for now in (float('nan'), float('inf'), -1, True, 'time'):
            with self.assertRaises(ValueError):
                state.start(self.db, self.thread, self.intent, now=now)
        row = state.start(self.db, self.thread, self.intent)
        with self.assertRaises(ValueError):
            state.accepted(self.db, row, 'unknown')

    def test_unknown_acceptance_recovery_adds_id_without_restoring_permission(self):
        row = self.begin()
        state.uncertain(self.db, row)
        recovered = state.pending(self.db)[0]
        state.accepted(self.db, recovered, self.queue)
        current = state.pending(self.db)[0]
        self.assertEqual(current['state'], 'unknown')
        self.assertEqual(current['queue_id'], self.queue)
        self.assertFalse(state.permitted(self.db, current))
        self.assertFalse(self.thread_row()['enabled'])
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_crash_after_acceptance_does_not_grant_resend(self):
        row = self.begin()
        state.accepted(self.db, row, self.queue)
        second = core.connection(pathlib.Path(self.temp.name) / 'state')
        self.addCleanup(second.close)
        current = state.pending(second)[0]
        self.assertEqual(current['queue_id'], self.queue)
        self.assertFalse(state.permitted(second, current))
        self.assertIsNone(state.start(second, self.thread, self.intent))

    def test_unknown_after_crash_can_record_real_turn_but_cannot_enable_thread(self):
        row = self.begin()
        state.uncertain(self.db, row)
        state.accepted(self.db, state.pending(self.db)[0], self.queue)
        state.reached(self.db, state.pending(self.db)[0], self.turn)
        self.assertEqual(self.thread_row()['state'], 'blocked')
        self.assertFalse(self.thread_row()['enabled'])
        self.assertEqual(self.db.execute('SELECT resume_turn FROM attempts').fetchone()[0], self.turn)

    def test_terminal_receipt_cannot_be_replaced_by_unknown_or_cancelled(self):
        row = self.begin()
        state.reached(self.db, row, self.turn)
        self.assertFalse(state.uncertain(self.db, row))
        self.assertFalse(state.cancelled(self.db, row))
        self.assertEqual(self.thread_row()['state'], 'monitoring')

    def test_record_receipt_must_confirm_the_same_actual_turn(self):
        row = self.begin()
        with mock.patch('core.record_receipt', return_value=None):
            with self.assertRaises(ValueError):
                state.reached(self.db, row, self.turn)
        self.assertEqual(state.pending(self.db)[0]['state'], 'sending')

    def test_failed_nested_write_does_not_rollback_outer_work(self):
        row = self.begin()
        self.db.execute('BEGIN IMMEDIATE')
        self.db.execute("UPDATE threads SET title='外側' WHERE id=?", (self.thread,))
        with mock.patch('core.record_receipt', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError):
                state.reached(self.db, row, self.turn)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.thread_row()['title'], '外側')
        self.assertEqual(state.pending(self.db)[0]['state'], 'sending')
        self.db.execute('ROLLBACK')

    def test_missing_or_incompatible_table_is_not_false_empty(self):
        self.db.execute('CREATE TABLE queue_dispatches (unexpected TEXT)')
        with self.assertRaises(ValueError):
            state.pending(self.db)
        with self.assertRaises(ValueError):
            state.initialise(self.db)

    def test_orphan_dispatch_is_not_silently_hidden(self):
        self.begin()
        self.db.execute('DELETE FROM attempts')
        with self.assertRaises(ValueError):
            state.pending(self.db)

    def test_off_on_generation_does_not_restore_old_dispatch(self):
        row = self.begin()
        auto_watch.set_enabled(self.db, False)
        auto_watch.set_enabled(self.db, True)
        self.assertFalse(state.permitted(self.db, row))
        state.reached(self.db, row, self.turn)
        self.assertEqual(self.thread_row()['state'], 'blocked')

    def test_send_receipt_requires_current_attempt_binding(self):
        row = self.begin()
        self.db.execute('UPDATE attempts SET thread_id=?', (str(uuid.uuid4()),))
        with self.assertRaises(ValueError):
            state.reached(self.db, row, self.turn)

    def test_repeated_start_rejects_modified_policy_or_generation_types(self):
        self.begin()
        with self.assertRaises(ValueError):
            state.start(self.db, self.thread, (*self.intent[:3], 999))
        with self.assertRaises(ValueError):
            state.start(self.db, self.thread, (self.intent[0], float(self.intent[1]), *self.intent[2:]))


if __name__ == '__main__':
    unittest.main()
