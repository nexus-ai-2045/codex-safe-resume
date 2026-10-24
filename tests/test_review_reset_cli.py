"""実データやGUIを使わず、CLIの確認世代と選び直し境界を検証する。"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import app
import auto_watch
import core
import watch


ROOT = Path(__file__).resolve().parents[1]
TID = '123e4567-e89b-12d3-a456-426614174000'


class ReviewResetCli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        core.sync(self.db, [dict(id=TID, title='fixture', cwd='', limited=False)])
        self.db.execute("UPDATE threads SET state='blocked',enabled=0,generation=7,"
                        "failure_turn='failure',resume_turn='receipt',claimed_at=1800000000")
        self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                        (TID, 'failure', 'attempt', 6, 'received', 'receipt'))

    def snapshot(self):
        return (core.list_threads(self.db),
                [tuple(row) for row in self.db.execute('SELECT * FROM attempts')])

    def invoke(self, *args, discover_result=None, expect_discovery=False):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['app.py', *args]), \
                patch('app.connection', return_value=self.db), \
                patch('app.discover', return_value=discover_result or ([], None)) as discover, \
                patch('app.GUI') as gui, redirect_stdout(out), redirect_stderr(err):
            try:
                app.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        if expect_discovery:
            discover.assert_called_once_with()
        else:
            discover.assert_not_called()
        gui.assert_not_called()
        return code, out.getvalue(), err.getvalue()

    def test_missing_stale_and_invalid_generation_preserve_state_and_ledger(self):
        before = self.snapshot()
        for token in (None, '6', '8', '-1', 'not-an-int', '1.5'):
            with self.subTest(token=token):
                args = ['mark', TID, 'idle']
                if token is not None:
                    args.extend(['--expected-generation', token])
                result = self.invoke(*args)
                self.assertNotEqual(result[0], 0)
                self.assertTrue(result[2])
                self.assertEqual(self.snapshot(), before)

    def test_preview_is_anonymous_and_preserves_state_and_ledger(self):
        self.db.execute("UPDATE threads SET title='private-title',cwd='/private/fixture'")
        before = self.snapshot()
        changes = self.db.total_changes
        code, output, error = self.invoke('mark', TID, 'idle', '--dry-run')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), {'状態': 'blocked', '選択': False,
                                            '世代': 7, '選び直し可能': True})
        for private in (TID, 'private-title', '/private/fixture', 'failure', 'receipt'):
            self.assertNotIn(private, output)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.db.total_changes, changes)

    def test_preview_reports_ineligible_state_without_changes(self):
        for state, enabled in (('claimed', 0), ('monitoring', 1), ('blocked', 1)):
            with self.subTest(state=state, enabled=enabled):
                self.db.execute('UPDATE threads SET state=?,enabled=?', (state, enabled))
                before = self.snapshot()
                code, output, error = self.invoke('mark', TID, 'idle', '--dry-run')
                self.assertEqual(code, 0, error)
                self.assertFalse(json.loads(output)['選び直し可能'])
                self.assertEqual(self.snapshot(), before)

    def test_preview_rejects_missing_or_invalid_target_without_changes(self):
        before = self.snapshot()
        for tid in ('223e4567-e89b-12d3-a456-426614174000', 'not-an-id'):
            with self.subTest(tid=tid):
                code, output, error = self.invoke('mark', tid, 'idle', '--dry-run')
                self.assertNotEqual(code, 0)
                self.assertNotIn(tid, output + error)
                self.assertEqual(self.snapshot(), before)

    def test_preview_and_generation_are_mutually_exclusive(self):
        before = self.snapshot()
        self.assertNotEqual(self.invoke('mark', TID, 'idle', '--dry-run',
                                        '--expected-generation', '7')[0], 0)
        self.assertEqual(self.snapshot(), before)

    def test_generation_option_is_rejected_for_other_statuses(self):
        before = self.snapshot()
        for status in ('blocked', 'completed'):
            with self.subTest(status=status):
                self.assertNotEqual(self.invoke('mark', TID, status, '--expected-generation', '7')[0], 0)
                self.assertEqual(self.snapshot(), before)
                self.assertNotEqual(self.invoke('mark', TID, status, '--dry-run')[0], 0)
                self.assertEqual(self.snapshot(), before)

    def test_completed_and_blocked_keep_their_existing_cli(self):
        attempts = self.snapshot()[1]
        for status in ('completed', 'blocked'):
            with self.subTest(status=status):
                self.assertEqual(self.invoke('mark', TID, status, expect_discovery=True)[0], 0)
                self.assertEqual(self.snapshot()[0][0]['state'], status)
                self.assertEqual(self.snapshot()[1], attempts)

    def test_terminal_mark_discovers_unsynced_desktop_target(self):
        attempts = self.snapshot()[1]
        for prefix, status in (('2', 'completed'), ('3', 'blocked')):
            tid = prefix + TID[1:]
            found = dict(id=tid, title='discovered-fixture', cwd='', limited=False)
            with self.subTest(status=status):
                self.assertIsNone(self.db.execute('SELECT state FROM threads WHERE id=?', (tid,)).fetchone())
                code, _, error = self.invoke('mark', tid, status,
                                             discover_result=([found], None), expect_discovery=True)
                self.assertEqual(code, 0, error)
                row = self.db.execute('SELECT state,enabled FROM threads WHERE id=?', (tid,)).fetchone()
                self.assertEqual(tuple(row), (status, 0))
                self.assertEqual(self.snapshot()[1], attempts)

    def test_terminal_mark_missing_target_is_an_error(self):
        tid = '223e4567-e89b-12d3-a456-426614174000'
        before = self.snapshot()
        for status in ('completed', 'blocked'):
            with self.subTest(status=status):
                code, _, error = self.invoke('mark', tid, status, expect_discovery=True)
                self.assertNotEqual(code, 0)
                self.assertTrue(error)
                self.assertEqual(self.snapshot(), before)
                with self.assertRaises(ValueError):
                    core.mark(self.db, tid, status)

    def test_terminal_invalid_id_is_rejected_before_discovery(self):
        before = self.snapshot()
        for status in ('completed', 'blocked'):
            self.assertNotEqual(self.invoke('mark', 'not-an-id', status)[0], 0)
            self.assertEqual(self.snapshot(), before)

    def test_matching_generation_resets_without_enabling_or_erasing_attempts(self):
        attempts = self.snapshot()[1]
        self.assertEqual(self.invoke('mark', TID, 'idle', '--expected-generation', '7')[0], 0)
        row = self.snapshot()[0][0]
        self.assertEqual(row['state'], 'idle')
        self.assertEqual(row['generation'], 8)
        self.assertFalse(row['enabled'])
        self.assertEqual(row['auto_excluded'], 1)
        self.assertIsNone(row['claimed_at'])
        self.assertIsNone(row['resume_turn'])
        self.assertEqual(self.snapshot()[1], attempts)

    def test_real_cli_help_and_reset_use_only_temporary_home(self):
        env = dict(os.environ, CODEX_RESUME_HOME=str(self.home),
                   CODEX_HOME=str(Path(self.temp.name) / 'unused-codex'), PYTHONIOENCODING='utf-8')
        command = [sys.executable, str(ROOT / 'app.py')]
        help_result = subprocess.run(command + ['mark', '--help'], env=env,
                                     capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('--expected-generation', help_result.stdout)
        self.assertIn('--dry-run', help_result.stdout)
        self.assertIn('idle', help_result.stdout)
        self.assertIn('確認', help_result.stdout)
        before = self.snapshot()
        preview = subprocess.run(command + ['mark', TID, 'idle', '--dry-run'],
                                 env=env, capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        generation = json.loads(preview.stdout)['世代']
        self.assertEqual(self.snapshot(), before)
        attempts = before[1]
        result = subprocess.run(command + ['mark', TID, 'idle', '--expected-generation', str(generation)],
                                env=env, capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot()[0][0]['state'], 'idle')
        self.assertFalse(self.snapshot()[0][0]['enabled'])
        self.assertEqual(self.snapshot()[0][0]['auto_excluded'], 1)
        self.assertEqual(self.snapshot()[1], attempts)
        self.assertFalse(Path(env['CODEX_HOME']).exists())

    def test_コマンドの選び直し後も全体監視で再登録せず明示選択を待つ(self):
        auto_watch.initialize(self.db)
        auto_watch.set_enabled(self.db, True)
        current = dict(id=TID, title='選び直す対象', cwd='', limited=True,
                       failure_turn='fresh-failure', latest_turn='fresh-failure',
                       turn_status='failed', observation_verified=True, awaiting=False)
        for terminal in ('blocked', 'completed'):
            with self.subTest(state=terminal):
                self.db.execute("UPDATE threads SET state='monitoring',enabled=1,auto_excluded=0")
                if terminal == 'blocked':
                    watch.block(self.db, TID, '手動確認が必要です')
                else:
                    core.mark(self.db, TID, 'completed', automatic=True)
                before = self.snapshot()
                generation = before[0][0]['generation']
                code, _, error = self.invoke('mark', TID, 'idle', '--expected-generation', str(generation))
                self.assertEqual(code, 0, error)
                row = self.snapshot()[0][0]
                self.assertEqual((row['state'], row['enabled'], row['auto_excluded']), ('idle', 0, 1))
                self.assertEqual(row['generation'], generation + 1)
                self.assertEqual(self.snapshot()[1], before[1])
                self.assertFalse(auto_watch.eligible(self.db, current))
                self.assertFalse(auto_watch.enrol(self.db, current, auto_watch.status(self.db)))
                auto_watch.set_enabled(self.db, False)
                auto_watch.set_enabled(self.db, True)
                self.assertFalse(auto_watch.eligible(self.db, current))
                code, _, error = self.invoke('enable', TID, discover_result=([current], None),
                                             expect_discovery=True)
                self.assertEqual(code, 0, error)
                self.assertEqual((self.snapshot()[0][0]['enabled'], self.snapshot()[0][0]['auto_excluded']), (1, 0))
                self.assertTrue(auto_watch.eligible(self.db, current))
                self.assertEqual(self.snapshot()[1], before[1])
