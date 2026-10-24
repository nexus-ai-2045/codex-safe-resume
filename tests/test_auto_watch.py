"""全体ON/OFFと自動登録の合成DB・模擬bridge契約。実送信なし。"""
from contextlib import closing
import pathlib
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

import core
import watch
import auto_watch

T = '123e4567-e89b-12d3-a456-426614174000'
NOW = 1800000000


def item(turn='failure', **kwargs):
    return dict(dict(id=T, title='合成チャット', cwd='', limited=True,
                     failure_turn=turn, latest_turn=turn, turn_status='failed',
                     observation_verified=True, awaiting=False), **kwargs)


class Bridge:
    def __init__(self):
        self.current = item()
        self.sent = []
        self.on_snapshot = None
        self.calls = 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def capabilities(self): return {'desktop_binding': True, 'safe_admission': True}
    def quota(self):
        return {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
    def snapshot(self, tid):
        self.calls += 1
        if self.on_snapshot: self.on_snapshot(self.calls)
        return dict(self.current, id=tid)
    def send_once(self, tid, expected, *, attempt_id=None):
        self.sent.append((tid, expected))
        return {'turn': {'id': 'resume-' + expected}}


class AutoWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / 'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        self.bridge = Bridge()
    def row(self): return core.list_threads(self.db)[0]
    def start(self):
        auto_watch.initialize(self.db)
        auto_watch.set_enabled(self.db, True)
    def tick(self, discovered=None, now=NOW):
        with patch('watch.discover', return_value=(discovered or [self.bridge.current], None)), \
                patch('core.time.time', return_value=now):
            return watch.once(self.db, lambda: self.bridge)
    def test_gui_initialization_is_off_once_and_keeps_attempts(self):
        core.sync(self.db, [item()]); core.enable(self.db, T, True)
        core.quota(self.db, T, 'failure', NOW-1); core.ready(self.db, [item()], NOW)
        core.record_intent(self.db, T)
        attempts = [tuple(r) for r in self.db.execute('SELECT * FROM attempts')]
        generation = self.row()['generation']
        self.assertIsNone(auto_watch.status(self.db))
        auto_watch.initialize(self.db)
        self.assertFalse(auto_watch.status(self.db)['enabled'])
        self.assertFalse(self.row()['enabled'])
        self.assertGreater(self.row()['generation'], generation)
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM attempts')], attempts)
        self.start()
        before = auto_watch.status(self.db)
        auto_watch.initialize(self.db)
        self.assertEqual(auto_watch.status(self.db), before)
    def test_global_off_does_not_connect_even_if_cli_selected(self):
        auto_watch.initialize(self.db)
        core.sync(self.db, [item()]); core.enable(self.db, T, True)
        with patch('watch.discover') as discover:
            self.assertEqual(watch.once(self.db, lambda: self.fail('connected'))['state'], 'disabled')
        discover.assert_not_called()
    def test_on_enrols_and_sends_unselected_verified_failure(self):
        self.start(); self.tick()
        self.assertEqual(self.bridge.sent, [(T, 'failure')])
        self.assertEqual(self.row()['state'], 'monitoring')
    def test_local_candidate_must_match_live_snapshot(self):
        self.start()
        self.bridge.current = item('changed')
        self.tick([item()])
        self.assertEqual(self.bridge.sent, [])
        self.assertFalse(self.row()['enabled'])
    def test_manual_disable_survives_global_toggle(self):
        self.start(); core.sync(self.db, [item()]); core.enable(self.db, T, False)
        auto_watch.set_enabled(self.db, False); auto_watch.set_enabled(self.db, True)
        self.tick()
        self.assertEqual(self.bridge.sent, [])
        self.assertTrue(self.row()['auto_excluded'])
        core.enable(self.db, T, True)
        self.assertFalse(self.row()['auto_excluded'])
        self.tick(); self.assertEqual(len(self.bridge.sent), 1)
    def test_off_does_not_create_manual_exclusion(self):
        self.start(); core.sync(self.db, [item()]); core.enable(self.db, T, True)
        auto_watch.set_enabled(self.db, False)
        self.assertFalse(self.row()['auto_excluded'])
        auto_watch.set_enabled(self.db, True)
        self.tick(); self.assertEqual(len(self.bridge.sent), 1)
    def test_same_failure_or_unknown_attempt_never_registers(self):
        self.start(); core.sync(self.db, [item()])
        for state, failure in [('received', 'failure'), ('sending', 'older-failure')]:
            with self.subTest(state=state):
                self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                                (T, failure, state, 0, state, 'receipt' if state == 'received' else None))
                self.tick()
                self.assertEqual(self.bridge.sent, [])
                self.assertFalse(self.row()['enabled'])
    def test_past_resume_turn_is_not_a_new_automatic_candidate(self):
        self.start(); core.sync(self.db, [item()])
        self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                        (T, 'past-failure', 'past-attempt', 0, 'received', 'past-resume'))
        self.bridge.current = item('past-resume')
        self.tick()
        self.assertEqual(self.bridge.sent, [])
        self.assertFalse(self.row()['enabled'])
    def test_global_monitoring_relimit_requires_exact_receipt(self):
        self.start(); self.tick()
        self.bridge.current = item('resume-failure')
        self.tick()
        self.assertEqual(self.bridge.sent, [(T, 'failure'), (T, 'resume-failure')])
        self.bridge.current = item('unrelated-failure')
        self.tick()
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(len(self.bridge.sent), 2)
    def test_off_on_continues_receipt_monitoring_then_relimit(self):
        self.start(); self.tick()
        auto_watch.set_enabled(self.db, False)
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(self.tick()['state'], 'disabled')
        auto_watch.set_enabled(self.db, True)
        self.bridge.current = item('resume-failure', limited=False, failure_turn=None,
                                   turn_status='inProgress')
        self.tick()
        self.assertEqual(self.row()['state'], 'monitoring')
        self.assertTrue(self.row()['enabled'])
        self.assertEqual(len(self.bridge.sent), 1)
        self.bridge.current = item('resume-failure')
        self.tick()
        self.assertEqual(self.bridge.sent, [(T, 'failure'), (T, 'resume-failure')])
    def test_off_on_continues_receipt_waiting_second_failure(self):
        self.start(); self.tick()
        self.bridge.current = item('resume-failure')
        self.bridge.quota = lambda: {'rateLimits': {
            'primary': {'usedPercent': 100, 'resetsAt': NOW+10},
            'secondary': {'usedPercent': 0}}}
        self.assertEqual(self.tick()['state'], 'quota-wait')
        self.assertEqual(self.row()['state'], 'waiting')
        auto_watch.set_enabled(self.db, False)
        auto_watch.set_enabled(self.db, True)
        self.bridge.quota = Bridge.quota.__get__(self.bridge)
        self.tick(now=NOW+11)
        self.assertEqual(self.bridge.sent, [(T, 'failure'), (T, 'resume-failure')])
    def test_off_on_does_not_revive_untrusted_receipt_series(self):
        for exclusion in ('manual', 'blocked', 'unknown', 'missing-receipt', 'unrelated'):
            with self.subTest(exclusion=exclusion):
                self.db.execute('DELETE FROM attempts')
                self.db.execute('DELETE FROM threads')
                self.bridge.current = item(); self.bridge.sent.clear()
                self.start(); self.tick()
                if exclusion == 'manual':
                    core.enable(self.db, T, False)
                elif exclusion == 'blocked':
                    core.mark(self.db, T, 'blocked')
                elif exclusion == 'unknown':
                    self.db.execute("UPDATE attempts SET state='sending'")
                elif exclusion == 'missing-receipt':
                    self.db.execute('UPDATE attempts SET resume_turn=NULL')
                else:
                    self.db.execute("UPDATE threads SET resume_turn='unrelated'")
                auto_watch.set_enabled(self.db, False)
                auto_watch.set_enabled(self.db, True)
                self.assertFalse(self.row()['enabled'])
                self.bridge.current = item('resume-failure')
                self.tick()
                self.assertEqual(len(self.bridge.sent), 1)
    def test_unverified_or_awaiting_snapshot_does_not_enable(self):
        self.start()
        for change in ({'observation_verified': False}, {'awaiting': True}, {'awaiting': None},
                       {'limited': None}, {'turn_status': 'unknown'}):
            with self.subTest(change=change):
                # Fresh local candidate each time; blocked observations remain stopped.
                self.db.execute("UPDATE threads SET state='idle',enabled=0")
                self.bridge.current = item(**change)
                self.tick([item()])
                self.assertEqual(self.bridge.sent, [])
                self.assertFalse(self.row()['enabled'])
    def test_initialize_on_off_preserve_received_attempts(self):
        self.start(); self.tick()
        attempts = [tuple(r) for r in self.db.execute('SELECT * FROM attempts')]
        for action in (lambda: auto_watch.initialize(self.db),
                       lambda: auto_watch.set_enabled(self.db, False),
                       lambda: auto_watch.set_enabled(self.db, True)):
            action()
            self.assertEqual([tuple(r) for r in self.db.execute('SELECT * FROM attempts')], attempts)
    def test_blocked_and_manual_completed_never_revive(self):
        self.start(); core.sync(self.db, [item()])
        for terminal in ('blocked', 'completed'):
            core.mark(self.db, T, terminal)
            self.tick(); self.assertEqual(self.bridge.sent, [])
    def test_監視停止と自動完了の選び直しは明示選択まで再登録しない(self):
        for terminal in ('blocked', 'completed'):
            with self.subTest(state=terminal):
                self.db.execute('DELETE FROM attempts')
                self.db.execute('DELETE FROM threads')
                self.bridge = Bridge()
                self.start()
                if terminal == 'blocked':
                    core.sync(self.db, [item()])
                    core.enable(self.db, T, True)
                    watch.block(self.db, T, '手動確認が必要です')
                else:
                    self.tick()
                    self.bridge.current = item('resume-failure', limited=False,
                                               failure_turn=None, turn_status='completed')
                    self.tick()
                    self.bridge.current = item('fresh-failure')
                self.assertEqual(self.row()['state'], terminal)
                self.assertEqual((self.row()['enabled'], self.row()['auto_excluded']), (0, 0))
                attempts = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
                sent = list(self.bridge.sent)
                generation = self.row()['generation']
                core.mark(self.db, T, 'idle', expected_generation=generation)
                self.assertEqual(self.row()['state'], 'idle')
                self.assertEqual(self.row()['generation'], generation + 1)
                self.assertEqual((self.row()['enabled'], self.row()['auto_excluded']), (0, 1))
                self.assertFalse(auto_watch.eligible(self.db, self.bridge.current))
                self.assertFalse(auto_watch.enrol(self.db, self.bridge.current, auto_watch.status(self.db)))
                self.tick()
                auto_watch.set_enabled(self.db, False)
                auto_watch.set_enabled(self.db, True)
                self.tick()
                self.assertEqual(self.bridge.sent, sent)
                self.assertEqual((self.row()['enabled'], self.row()['auto_excluded']), (0, 1))
                self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], attempts)
                core.enable(self.db, T, True)
                self.assertTrue(auto_watch.eligible(self.db, self.bridge.current))
                self.tick()
                self.assertEqual(self.bridge.sent, sent + [(T, self.bridge.current['failure_turn'])])
    def test_受領後の状態復帰と自動完了は個別除外を解除しない(self):
        for terminal in ('idle', 'completed'):
            with self.subTest(state=terminal):
                self.db.execute('DELETE FROM attempts')
                self.db.execute('DELETE FROM threads')
                self.bridge = Bridge()
                self.start(); self.tick()
                core.enable(self.db, T, False)
                attempts = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
                self.bridge.current = (item('resume-failure') if terminal == 'idle' else
                                       item('resume-failure', limited=False, failure_turn=None,
                                            turn_status='completed'))
                self.tick()
                self.assertEqual(self.row()['state'], terminal)
                self.assertEqual((self.row()['enabled'], self.row()['auto_excluded']), (0, 1))
                self.bridge.current = item('fresh-failure')
                self.assertFalse(auto_watch.eligible(self.db, self.bridge.current))
                self.assertFalse(auto_watch.enrol(self.db, self.bridge.current, auto_watch.status(self.db)))
                auto_watch.set_enabled(self.db, False)
                auto_watch.set_enabled(self.db, True)
                self.tick()
                self.assertEqual(self.bridge.sent, [(T, 'failure')])
                self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], attempts)
    def test_自動完了の候補を観測中に除外したら待機状態へ戻さない(self):
        self.start(); self.tick()
        self.bridge.current = item('resume-failure', limited=False,
                                   failure_turn=None, turn_status='completed')
        self.tick()
        self.bridge.current = item('fresh-failure')
        self.assertTrue(auto_watch.eligible(self.db, self.bridge.current))
        attempts = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
        self.bridge.on_snapshot = lambda count: core.enable(self.db, T, False)
        self.tick()
        self.assertEqual(self.row()['state'], 'completed')
        self.assertEqual((self.row()['enabled'], self.row()['auto_excluded']), (0, 1))
        self.assertEqual(self.bridge.sent, [(T, 'failure')])
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], attempts)
    def test_automatic_completed_accepts_only_new_verified_failure(self):
        self.start(); self.tick()
        self.bridge.current = item('resume-failure', limited=False, failure_turn=None, turn_status='completed')
        self.tick()
        self.assertEqual(self.row()['state'], 'completed')
        self.assertEqual(self.row()['failure_turn'], 'failure')
        self.assertEqual(self.row()['resume_turn'], 'resume-failure')
        self.assertFalse(self.row()['auto_excluded'])
        for turn in ('failure', 'resume-failure'):
            self.bridge.current = item(turn); self.tick()
            self.assertEqual(len(self.bridge.sent), 1)
        self.bridge.current = item('fresh-failure'); self.tick()
        self.assertEqual(self.bridge.sent[-1], (T, 'fresh-failure'))
        self.assertEqual(len(self.bridge.sent), 2)
    def test_archived_subagent_and_unknown_candidates_are_rejected(self):
        self.start()
        for change in ({'excluded_reason': 'archived'}, {'excluded_reason': 'subagent'},
                       {'limited': None}, {'turn_status': 'unknown'}, {'failure_turn': None}):
            self.tick([item(**change)])
            self.assertEqual(self.bridge.sent, [])
    def test_off_at_intent_boundary_keeps_durable_attempt_without_send(self):
        self.start()
        def toggle(count):
            if count == 2: auto_watch.set_enabled(self.db, False)
        self.bridge.on_snapshot = toggle
        self.tick()
        self.assertEqual(self.bridge.sent, [])
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(self.db.execute('SELECT state FROM attempts').fetchone()[0], 'sending')
    def test_off_on_at_intent_boundary_invalidates_generation(self):
        self.start()
        def toggle(count):
            if count == 2:
                auto_watch.set_enabled(self.db, False)
                auto_watch.set_enabled(self.db, True)
        self.bridge.on_snapshot = toggle
        self.tick(); self.assertEqual(self.bridge.sent, [])
    def test_toggle_during_candidate_snapshot_does_not_reenrol(self):
        self.start()
        def toggle(count):
            auto_watch.set_enabled(self.db, False)
            auto_watch.set_enabled(self.db, True)
        self.bridge.on_snapshot = toggle
        self.tick(); self.assertEqual(self.bridge.sent, [])
    def test_off_cannot_commit_inside_send_transaction(self):
        self.start(); outcomes = []
        original = self.bridge.send_once
        def send(*args, **kwargs):
            with closing(sqlite3.connect(self.home/'resume.sqlite3', timeout=.01, isolation_level=None)) as other:
                try: auto_watch.set_enabled(other, False)
                except sqlite3.OperationalError: outcomes.append('locked')
            return original(*args, **kwargs)
        self.bridge.send_once = send
        self.tick()
        self.assertEqual(outcomes, ['locked'])
        self.assertEqual(len(self.bridge.sent), 1)
    def test_all_normal_chats_beyond_2000_use_existing_reader(self):
        self.start()
        source = pathlib.Path(self.tmp.name)/'codex'; source.mkdir()
        ids = [str(uuid.UUID(int=i+1)) for i in range(2101)]
        with closing(sqlite3.connect(source/'state_5.sqlite')) as db:
            db.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,source TEXT,archived INTEGER,updated_at INTEGER)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?)',
                           [(tid, '合成', '', 'cli', 0, 2101-i) for i, tid in enumerate(ids)])
            db.commit()
        with closing(sqlite3.connect(source/'thread_history_1.sqlite')) as db:
            db.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT)')
            db.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',
                       (ids[-1], 'failure', 'failed', '{"code":"usageLimitExceeded"}'))
            db.commit()
        with patch('watch.discover', side_effect=lambda **kw: core.discover(source, **kw)), \
                patch('core.time.time', return_value=NOW):
            watch.once(self.db, lambda: self.bridge)
        self.assertEqual(self.bridge.sent, [(ids[-1], 'failure')])
    def test_legacy_cli_policy_absent_stays_selected_only(self):
        core.sync(self.db, [item()]); self.tick()
        self.assertEqual(self.bridge.sent, [])
        core.enable(self.db, T, True); self.tick()
        self.assertEqual(self.bridge.sent, [(T, 'failure')])
    def test_version3_migration_excludes_old_terminal_rows(self):
        core.sync(self.db, [item()])
        self.db.execute("UPDATE threads SET state='completed',auto_excluded=0")
        self.db.execute('ALTER TABLE threads DROP COLUMN auto_excluded')
        self.db.execute('DROP TABLE auto_watch_policy')
        self.db.execute('DROP TABLE desktop_sessions')
        self.db.execute('PRAGMA user_version=3')
        with closing(core.connection(self.home)) as migrated:
            self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(migrated.execute('SELECT auto_excluded FROM threads').fetchone()[0], 1)
            self.assertIsNone(auto_watch.status(migrated))


if __name__ == '__main__': unittest.main()
