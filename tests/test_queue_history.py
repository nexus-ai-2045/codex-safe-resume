import json
import contextlib
import os
import pathlib
import sqlite3
import stat
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from queue_history import QueueHistory, continuation_text


class QueueHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.thread = str(uuid.uuid4())
        self.turn = str(uuid.uuid4())
        self.attempt = str(uuid.uuid4())
        self.queue = str(uuid.uuid4())
        self.rollout = self.home / 'sessions' / ('rollout-' + self.thread + '.jsonl')
        self.rollout.parent.mkdir()
        self.rollout.write_text('{}\n', encoding='utf-8')
        self.execute('state_5.sqlite', 'CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)')
        self.execute('state_5.sqlite', 'INSERT INTO threads VALUES (?,?,0)', (self.thread, str(self.rollout)))
        self.execute('thread_history_1.sqlite', 'CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT, error_json TEXT, first_user_item_id TEXT)')
        self.execute('thread_history_1.sqlite', 'CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, item_type TEXT, item_json TEXT)')
        self.execute('thread_history_1.sqlite', 'CREATE TABLE thread_history_projection_state (thread_id TEXT, next_rollout_byte_offset INTEGER)')
        self.execute('thread_history_1.sqlite', 'INSERT INTO thread_history_projection_state VALUES (?,?)', (self.thread, self.rollout.stat().st_size))
        self.execute('queue_1.sqlite', 'CREATE TABLE queued_items (id TEXT, thread_id TEXT, payload_json TEXT)')
        self.history = QueueHistory(self.home)

    def execute(self, database, statement, parameters=()):
        with contextlib.closing(sqlite3.connect(self.home / database)) as db:
            with db:
                db.execute(statement, parameters)

    def add_turn(self, text=None, *, first='item-first', item='item-first', thread=None, turn=None, status='completed', ordinal=1, client=None):
        thread = thread or self.thread
        turn = turn or self.turn
        payload = {'id': item, 'type': 'userMessage', 'clientId': client, 'content': [{'type': 'text', 'text': text or continuation_text(self.attempt)}]}
        self.execute('thread_history_1.sqlite', 'INSERT INTO thread_turns VALUES (?,?,?,?,?,?)', (thread, turn, ordinal, status, None, first))
        self.execute('thread_history_1.sqlite', 'INSERT INTO thread_items VALUES (?,?,?,?,?)', (thread, turn, item, 'userMessage', json.dumps(payload)))

    def enqueue(self, text=None, client=None):
        payload = {'UserInput': {'content': [{'type': 'text', 'text': text or continuation_text(self.attempt)}], 'client_id': client}}
        self.execute('queue_1.sqlite', 'INSERT INTO queued_items VALUES (?,?,?)', (self.queue, self.thread, json.dumps(payload)))

    def test_exact_first_user_message_is_reached_with_actual_turn(self):
        self.add_turn()
        self.assertEqual(self.history.receipt(self.thread, self.attempt, self.queue), {'delivery': 'reached', 'turn': {'id': self.turn}})
        self.assertNotEqual(self.turn, self.queue)

    def test_pending_is_not_turn_receipt(self):
        self.enqueue()
        self.assertEqual(self.history.receipt(self.thread, self.attempt, self.queue), {'delivery': 'pending'})
        row = self.history.pending(self.thread)[0]
        self.assertEqual(row['queue_id'], self.queue)
        self.assertEqual(row['input'], [{'type': 'text', 'text': continuation_text(self.attempt)}])

    def test_quoted_partial_or_modified_marker_is_unknown(self):
        for text in ('引用: ' + continuation_text(self.attempt), continuation_text(self.attempt) + ' extra', '[Safe Resume: ' + self.attempt + ']'):
            with self.subTest(text=text):
                self.execute('thread_history_1.sqlite', 'DELETE FROM thread_items')
                self.execute('thread_history_1.sqlite', 'DELETE FROM thread_turns')
                self.add_turn(text)
                self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_other_thread_and_non_first_message_are_unknown(self):
        self.add_turn(thread=str(uuid.uuid4()))
        self.add_turn(first='other-item')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_multiple_matches_are_unknown(self):
        self.add_turn()
        self.add_turn(turn=str(uuid.uuid4()), item='second', first='second', ordinal=2)
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_stale_projection_cannot_prove_delivery_or_candidate(self):
        self.add_turn(status='failed')
        self.rollout.write_bytes(b'{}\n{}\n')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')
        self.assertFalse(self.history.candidate(self.thread)['projection_current'])

    def test_candidate_is_only_local_observation(self):
        self.add_turn(status='failed')
        result = self.history.candidate(self.thread)
        self.assertTrue(result['projection_current'])
        self.assertFalse(result['observation_verified'])
        self.assertEqual(result['latest_turn'], self.turn)
        self.assertEqual(result['status'], 'failed')

    def test_queue_id_does_not_prove_delivery(self):
        self.add_turn('続けて', client=self.queue)
        self.assertEqual(self.history.receipt(self.thread, self.attempt, self.queue)['delivery'], 'unknown')

    def test_attempt_client_id_is_reached_without_marker(self):
        self.add_turn('続けて', client=self.attempt)
        self.assertEqual(self.history.receipt(self.thread, self.attempt), {'delivery': 'reached', 'turn': {'id': self.turn}})

    def test_explicit_client_id_is_not_queue_id(self):
        client = str(uuid.uuid4())
        self.add_turn('続けて', client=client)
        self.assertEqual(self.history.receipt(self.thread, self.attempt, self.queue, client)['delivery'], 'reached')

    def test_attempt_client_in_non_first_message_is_unknown(self):
        self.add_turn('続けて', client=self.attempt, first='different')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_marker_and_client_on_different_turns_are_ambiguous(self):
        self.add_turn()
        self.add_turn('続けて', client=self.attempt, turn=str(uuid.uuid4()), item='second', first='second', ordinal=2)
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_archived_and_foreign_rollout_rejected(self):
        self.add_turn()
        self.execute('state_5.sqlite', 'UPDATE threads SET archived=1')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')
        self.execute('state_5.sqlite', 'UPDATE threads SET archived=0, rollout_path=?', (str(self.home.parent / 'foreign.jsonl'),))
        self.assertFalse(self.history.candidate(self.thread)['projection_current'])

    def test_unknown_newer_schema_does_not_fallback(self):
        self.add_turn()
        self.execute('thread_history_2.sqlite', 'CREATE TABLE unsupported (id TEXT)')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_hardlinked_database_rejected(self):
        self.add_turn()
        os.link(self.home / 'thread_history_1.sqlite', self.home / 'duplicate.sqlite')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_corrupt_json_and_unknown_status_rejected(self):
        self.add_turn(status='surprise')
        self.assertFalse(self.history.candidate(self.thread)['projection_current'])
        self.execute('thread_history_1.sqlite', "UPDATE thread_turns SET status='completed'")
        self.execute('thread_history_1.sqlite', "UPDATE thread_items SET item_json='invalid'")
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_invalid_thread_and_attempt_fail_closed(self):
        self.assertEqual(self.history.receipt('../bad', self.attempt)['delivery'], 'unknown')
        self.assertEqual(self.history.receipt(self.thread, 'not-uuid')['delivery'], 'unknown')
        self.assertFalse(self.history.candidate('../bad')['projection_current'])

    def test_reading_does_not_modify_database(self):
        self.add_turn()
        before = {path.name: path.read_bytes() for path in self.home.glob('*.sqlite')}
        self.history.receipt(self.thread, self.attempt)
        self.history.candidate(self.thread)
        self.history.pending(self.thread)
        after = {path.name: path.read_bytes() for path in self.home.glob('*.sqlite')}
        self.assertEqual(before, after)

    def test_duplicate_pending_and_malformed_queue_are_not_accepted(self):
        self.enqueue()
        self.execute('queue_1.sqlite', 'INSERT INTO queued_items SELECT ?,thread_id,payload_json FROM queued_items', (str(uuid.uuid4()),))
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')
        self.execute('queue_1.sqlite', "UPDATE queued_items SET payload_json='{}'")
        with self.assertRaises(ValueError):
            self.history.pending(self.thread)

    def test_payload_and_database_size_limits(self):
        self.add_turn()
        with mock.patch('queue_history.MAX_JSON_BYTES', 20):
            self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')
        with mock.patch('queue_history.MAX_DATABASE_BYTES', 20):
            self.assertFalse(self.history.candidate(self.thread)['projection_current'])

    def test_existing_multi_gigabyte_history_is_not_read_into_memory_or_rejected(self):
        import queue_history
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1,
                                   st_uid=os.getuid() if hasattr(os, 'getuid') else 0,
                                   st_size=3 * 1024 ** 3, st_dev=1, st_ino=2, st_mtime_ns=3)
        path = mock.Mock()
        path.stat.return_value = metadata
        with mock.patch('queue_history.reject_links'):
            result = queue_history._identity(path, database=True)
        self.assertEqual(result, (1, 2, metadata.st_size, 3))
        path.open.assert_not_called()

    def test_traversal_in_rollout_path_rejected(self):
        path = self.home / 'sessions' / '..' / 'sessions' / self.rollout.name
        self.execute('state_5.sqlite', 'UPDATE threads SET rollout_path=?', (str(path),))
        self.assertFalse(self.history.candidate(self.thread)['projection_current'])

    def test_symlink_rollout_rejected(self):
        self.add_turn()
        other = self.home / 'sessions' / 'other.jsonl'
        self.rollout.rename(other)
        try:
            self.rollout.symlink_to(other)
        except OSError:
            self.skipTest('Symlink privilege unavailable')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_hardlinked_rollout_rejected(self):
        self.add_turn()
        os.link(self.rollout, self.home / 'sessions' / 'other.jsonl')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_database_view_is_not_supported_table(self):
        self.execute('thread_history_1.sqlite', 'DROP TABLE thread_turns')
        self.execute('thread_history_1.sqlite', "CREATE VIEW thread_turns AS SELECT NULL AS thread_id")
        self.assertFalse(self.history.candidate(self.thread)['projection_current'])

    def test_corrupt_database_is_unknown(self):
        (self.home / 'thread_history_1.sqlite').write_bytes(b'invalid database')
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_missing_item_identity_is_not_first_user_proof(self):
        self.add_turn(first=None, item=None)
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')

    def test_duplicate_json_keys_and_nonfinite_json_rejected(self):
        self.add_turn()
        payload = '{"type":"other","type":"userMessage","id":"item-first","clientId":"' + self.attempt + '","content":[]}'
        self.execute('thread_history_1.sqlite', 'UPDATE thread_items SET item_json=?', (payload,))
        self.assertEqual(self.history.receipt(self.thread, self.attempt)['delivery'], 'unknown')
        self.execute('thread_history_1.sqlite', "UPDATE thread_turns SET error_json='{\"value\":NaN}'")
        self.assertFalse(self.history.candidate(self.thread)['projection_current'])


if __name__ == '__main__':
    unittest.main()
