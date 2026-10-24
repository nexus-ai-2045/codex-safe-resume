"""再起動・競合・再制限・不明送信の安全契約。実Desktop送信は行わない。"""
from contextlib import closing
import os
import pathlib
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import core
import cli_security
import watch
from desktop_bridge import DesktopBridge, BridgeError

T = '123e4567-e89b-12d3-a456-426614174000'
NOW = 1800000000


def limits(percent=0):
    return {'rateLimits': {'primary': {'usedPercent': percent, 'resetsAt': NOW-1},
                           'secondary': {'usedPercent': 0, 'resetsAt': NOW-1}}}


def item(turn='failure', status='failed', limited=True):
    return dict(id=T, title='デモ', cwd='', limited=limited,
                failure_turn=turn if limited else None, latest_turn=turn,
                turn_status=status, awaiting=False, observation_verified=True)


class Bridge:
    sent = []
    current = item()
    response = {'turn': {'id': 'resumed'}}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def quota(self): return limits()
    def capabilities(self): return {'safe_admission': True, 'desktop_binding': True}
    def snapshot(self, tid): return dict(self.current)
    def send_once(self, tid, expected, *, attempt_id=None):
        self.sent.append((tid, expected))
        return self.response


class Safety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)/'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        core.sync(self.db, [item()]); core.enable(self.db, T, True)
        core.quota(self.db, T, 'failure', NOW-1)
        Bridge.sent = []; Bridge.current = item(); Bridge.response = {'turn': {'id': 'resumed'}}
    def tick(self, factory=Bridge):
        with patch('watch.discover', return_value=([dict(Bridge.current)], None)), patch('core.time.time', return_value=NOW):
            return watch.once(self.db, factory)
    def row(self):
        return next(row for row in core.list_threads(self.db) if row['id'] == T)
    def test_safety_fixture_tracks_target_id_even_when_other_row_is_newer(self):
        other = dict(item(), id='123e4567-e89b-12d3-a456-426614174001')
        core.sync(self.db, [other])
        self.db.executemany('UPDATE threads SET updated_at=? WHERE id=?',
                            [(NOW, T), (NOW + 1000, other['id'])])
        self.assertEqual(core.list_threads(self.db)[0]['id'], other['id'])
        self.assertEqual(self.row()['id'], T)
        self.check_missing_target_stops([other])

    def check_missing_target_stops(self, found):
        self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                        (T, 'older-failure', 'older-attempt', 0, 'received', 'older-receipt'))
        before = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
        with patch('watch.discover', return_value=(found, None)):
            watch.once(self.db, Bridge)
        self.assertEqual(Bridge.sent, [])
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertNotIn('アーカイブ', self.row()['last_error'])
        self.assertEqual(self.row()['failure_turn'], 'failure')
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], before)

    def test_hidden_missing_target_never_sends_even_with_valid_live_snapshot(self):
        other = dict(item(), id='123e4567-e89b-12d3-a456-426614174001')
        self.check_missing_target_stops([other])

    def test_empty_inventory_disables_unknown_saved_target_without_retry(self):
        self.check_missing_target_stops([])

    def test_persist_actual_receipt(self):
        self.tick()
        self.assertEqual(self.row()['resume_turn'], 'resumed')
        self.assertEqual(len(Bridge.sent), 1)
    def test_completed_receipt_stops(self):
        self.tick(); Bridge.current = item('resumed', 'completed', False)
        self.tick()
        self.assertEqual(self.row()['state'], 'completed')
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(len(Bridge.sent), 1)
    def test_approval_wait_stops(self):
        self.tick(); Bridge.current = item('resumed', 'inProgress', False)
        Bridge.current['awaiting'] = True
        self.tick()
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(len(Bridge.sent), 1)
    def test_unrelated_failure_never_rearms(self):
        self.tick(); Bridge.current = item('manual-turn')
        self.tick()
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(len(Bridge.sent), 1)
    def test_same_receipt_quota_failure_rearms(self):
        self.tick(); Bridge.current = item('resumed')
        self.tick()
        self.assertEqual(len(Bridge.sent), 2)
        self.assertEqual(Bridge.sent[-1], (T, 'resumed'))
    def test_missing_receipt_never_retries_after_restart(self):
        Bridge.response = {}
        self.tick()
        self.assertEqual(self.row()['state'], 'blocked')
        self.db.close(); self.db = core.connection(self.home); self.addCleanup(self.db.close)
        self.tick()
        self.assertEqual(len(Bridge.sent), 1)
    def test_stale_claim_never_retries(self):
        core.ready(self.db, [item()], now=NOW)
        self.tick()
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertEqual(Bridge.sent, [])
    def test_unknown_capabilities_never_sends(self):
        class Unsafe(Bridge):
            def capabilities(self): return {'safe_admission': False, 'desktop_binding': True}
        self.tick(Unsafe)
        self.assertEqual(Bridge.sent, [])
    def test_snapshot_change_after_claim_does_not_send(self):
        class Changed(Bridge):
            count = 0
            def snapshot(self, tid):
                self.count += 1
                return item() if self.count == 1 else item('manual', 'completed', False)
        self.tick(Changed)
        self.assertEqual(Bridge.sent, [])
    def test_disabling_at_send_boundary_prevents_send(self):
        parent = self
        class Disabled(Bridge):
            count = 0
            def snapshot(self, tid):
                self.count += 1
                if self.count == 2: core.enable(parent.db, T, False)
                return item()
        self.tick(Disabled)
        self.assertEqual(Bridge.sent, [])
    def test_disable_cannot_commit_inside_send_boundary(self):
        home = self.home
        outcomes = []
        class ConcurrentDisable(Bridge):
            def send_once(self, tid, expected, *, attempt_id=None):
                with closing(sqlite3.connect(home/'resume.sqlite3', timeout=0.01, isolation_level=None)) as other:
                    try:
                        core.enable(other, T, False)
                        outcomes.append('disabled')
                    except sqlite3.OperationalError:
                        outcomes.append('selection_locked')
                return super().send_once(tid, expected, attempt_id=attempt_id)
        self.tick(ConcurrentDisable)
        self.assertEqual(outcomes, ['selection_locked'])
        self.assertEqual(len(Bridge.sent), 1)
        self.assertTrue(self.row()['enabled'])
        self.assertEqual(self.row()['state'], 'monitoring')

    def test_partial_history_schema_is_unavailable_without_crash(self):
        codex = pathlib.Path(self.tmp.name)/'codex'; codex.mkdir()
        with closing(sqlite3.connect(codex/'state_5.sqlite')) as db:
            db.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?)', (T,'デモ','')); db.commit()
        with closing(sqlite3.connect(codex/'thread_history_1.sqlite')) as db:
            db.execute('CREATE TABLE thread_items(thread_id TEXT,item_type TEXT,item_json TEXT,rollout_ordinal INTEGER)')
        found, warning = core.discover(codex)
        self.assertIsNone(warning)
        self.assertFalse(found[0]['limited'])
        self.assertFalse(found[0]['observation_verified'])

    def test_bad_quota_does_not_arm(self):
        class Bad(Bridge):
            def quota(self): return limits(float('nan'))
        self.db.execute("UPDATE threads SET state='idle', reset_at=NULL")
        self.tick(Bad)
        self.assertIsNone(self.row()['reset_at'])
    def test_authoritative_quota_denial_or_unknown_never_sends(self):
        for field, value in (('ordinaryUsageAllowed', False), ('ordinaryUsageAllowed', None),
                             ('spendControlReached', True), ('spendControlReached', None)):
            with self.subTest(field=field, value=value):
                reply = limits()
                target = reply if field == 'ordinaryUsageAllowed' else reply['rateLimits']
                target[field] = value
                class Restricted(Bridge):
                    def quota(self): return reply
                self.tick(Restricted)
                self.assertEqual(Bridge.sent, [])
                self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_second_monitor_does_not_interleave(self):
        with core.monitor_lease(self.db) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.tick()['state'], 'busy')
        self.assertEqual(Bridge.sent, [])

    def test_review_reset_preserves_sending_and_received_attempts(self):
        for attempt_state in ('sending', 'received'):
            with self.subTest(attempt_state=attempt_state):
                self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                                (T, attempt_state, attempt_state, 1, attempt_state,
                                 'receipt' if attempt_state == 'received' else None))
        before = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
        for terminal in ('blocked', 'completed'):
            with self.subTest(state=terminal):
                core.mark(self.db, T, terminal)
                generation = self.row()['generation']
                core.mark(self.db, T, 'idle', expected_generation=self.row()['generation'])
                self.assertEqual(self.row()['generation'], generation + 1)
                self.assertFalse(self.row()['enabled'])
                self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], before)

    def test_review_reset_rejects_active_inflight_or_missing_rows_atomically(self):
        for state in ('idle', 'waiting', 'claimed', 'dispatched', 'monitoring', 'ready'):
            for enabled in (0, 1):
                with self.subTest(state=state, enabled=enabled):
                    self.db.execute('UPDATE threads SET state=?,enabled=?', (state, enabled))
                    before = self.row()
                    with self.assertRaises(ValueError):
                        core.mark(self.db, T, 'idle', expected_generation=self.row()['generation'])
                    self.assertEqual(self.row(), before)
        for state in ('blocked', 'completed'):
            self.db.execute('UPDATE threads SET state=?,enabled=1', (state,))
            before = self.row()
            with self.assertRaises(ValueError):
                core.mark(self.db, T, 'idle', expected_generation=self.row()['generation'])
            self.assertEqual(self.row(), before)
        with self.assertRaises(ValueError):
            core.mark(self.db, '223e4567-e89b-12d3-a456-426614174000', 'idle', expected_generation=0)

    def test_review_reset_requires_valid_current_generation(self):
        core.mark(self.db, T, 'blocked')
        before = self.row()
        for token in (None, True, False, -1, 1.0, '1', [], {}, before['generation'] - 1):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    core.mark(self.db, T, 'idle', expected_generation=token)
                self.assertEqual(self.row(), before)
        with self.assertRaises(ValueError):
            core.mark(self.db, T, 'idle')
        self.assertEqual(self.row(), before)

    def test_review_reset_does_not_replay_same_failure(self):
        core.ready(self.db, [item()], now=NOW)
        self.assertIsNotNone(core.record_intent(self.db, T))
        for attempt_state in ('sending', 'received'):
            with self.subTest(attempt_state=attempt_state):
                self.db.execute('UPDATE attempts SET state=?,resume_turn=?',
                                (attempt_state, 'receipt' if attempt_state == 'received' else None))
                before = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
                core.mark(self.db, T, 'blocked')
                core.mark(self.db, T, 'idle', expected_generation=self.row()['generation'])
                self.tick()
                self.assertEqual(Bridge.sent, [])
                core.enable(self.db, T, True)
                self.tick()
                self.assertEqual(Bridge.sent, [])
                self.assertEqual(self.row()['state'], 'blocked')
                self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], before)

    def test_review_reset_new_failure_still_requires_selection_and_validation(self):
        core.mark(self.db, T, 'completed')
        core.mark(self.db, T, 'idle', expected_generation=self.row()['generation'])
        Bridge.current = item('new-failure')
        self.tick()
        self.assertEqual(Bridge.sent, [])
        core.enable(self.db, T, True)
        class Unavailable(Bridge):
            def quota(self): return {'ordinaryUsageAllowed': None, **limits()}
        self.tick(Unavailable)
        self.assertEqual(Bridge.sent, [])
        self.tick()
        self.assertEqual(Bridge.sent, [(T, 'new-failure')])

    def test_previously_attempted_failure_is_not_sent_again(self):
        self.tick()
        self.db.execute("UPDATE threads SET state='waiting',enabled=1,resume_turn=NULL")
        self.tick()
        self.assertEqual(len(Bridge.sent), 1)
        self.assertEqual(self.row()['state'], 'blocked')

    def test_hardlinked_state_file_refused(self):
        alias = pathlib.Path(self.tmp.name)/'db-alias'
        import os
        os.link(self.home/'resume.sqlite3', alias)
        with self.assertRaises(RuntimeError): core.connection(self.home)

    def test_windows_hardlink_rejected_before_sqlite_open(self):
        from types import SimpleNamespace
        alias = pathlib.Path(self.tmp.name)/'windows-db-alias'
        import os
        os.link(self.home/'resume.sqlite3', alias)
        with patch('core.os', SimpleNamespace(name='nt')), patch('core.reject_links'), patch('core.sqlite3.connect') as connect:
            with self.assertRaises(RuntimeError):
                core.connection(self.home)
            connect.assert_not_called()

    def test_newer_schema_refused(self):
        self.db.execute('PRAGMA user_version=999')
        with self.assertRaises(RuntimeError): core.connection(self.home)
    def test_no_parent_symlink(self):
        parent = pathlib.Path(self.tmp.name)/'alias'
        if os.name == 'nt':
            subprocess.run(
                [str(cli_security._system_powershell()), '-NoProfile', '-NonInteractive',
                 '-Command', "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction -Path $env:CSR_TEST_JUNCTION_PATH -Target $env:CSR_TEST_JUNCTION_TARGET | Out-Null"],
                check=True, shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env=dict(os.environ, CSR_TEST_JUNCTION_PATH=str(parent),
                         CSR_TEST_JUNCTION_TARGET=str(self.home)))
            self.addCleanup(parent.rmdir)
            info = parent.lstat()
            self.assertTrue(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            self.assertEqual(info.st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
        else:
            parent.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(RuntimeError): core.connection(parent/'other')
        self.assertFalse((self.home/'other').exists())


class Quota(unittest.TestCase):
    def test_authoritative_denial_overrides_available_percentages(self):
        for mapped in (False, True):
            for field, value in (('ordinaryUsageAllowed', False), ('spendControlReached', True)):
                with self.subTest(mapped=mapped, field=field):
                    reply = limits(10)
                    selected = reply['rateLimits']
                    if mapped:
                        selected = limits(20)['rateLimits']
                        reply['rateLimitsByLimitId'] = {'codex': selected}
                        reply['rateLimits']['spendControlReached'] = False
                    reply['ordinaryUsageAllowed'] = True
                    selected['spendControlReached'] = False
                    target = reply if field == 'ordinaryUsageAllowed' else selected
                    target[field] = value
                    self.assertEqual(core.observe_quota(reply, require_both=True), (False, None))

    def test_unknown_or_malformed_authoritative_permission_is_rejected(self):
        for mapped in (False, True):
            for field in ('ordinaryUsageAllowed', 'spendControlReached'):
                for value in (None, 0, 1, 0.0, 1.0, '', 'false', 'true', [], {}):
                    with self.subTest(mapped=mapped, field=field, value=value):
                        reply = limits(10)
                        selected = reply['rateLimits']
                        if mapped:
                            selected = limits(20)['rateLimits']
                            reply['rateLimitsByLimitId'] = {'codex': selected}
                            reply['rateLimits']['spendControlReached'] = False
                        reply['ordinaryUsageAllowed'] = True
                        selected['spendControlReached'] = False
                        target = reply if field == 'ordinaryUsageAllowed' else selected
                        target[field] = value
                        with self.assertRaises(ValueError):
                            core.observe_quota(reply, require_both=True)

    def test_explicit_permission_and_legacy_omission_remain_supported(self):
        for mapped in (False, True):
            for explicit in (False, True):
                with self.subTest(mapped=mapped, explicit=explicit):
                    reply = limits(10)
                    selected = reply['rateLimits']
                    if mapped:
                        selected = limits(20)['rateLimits']
                        reply['rateLimitsByLimitId'] = {'codex': selected}
                        # The selected bucket is authoritative even when the
                        # legacy bucket disagrees about spend control.
                        reply['rateLimits']['spendControlReached'] = True
                    if explicit:
                        reply['ordinaryUsageAllowed'] = True
                        selected['spendControlReached'] = False
                    self.assertEqual(core.observe_quota(reply, require_both=True), (True, None))

    def test_permission_does_not_override_full_or_invalid_windows(self):
        reply = limits(100)
        reply['ordinaryUsageAllowed'] = True
        reply['rateLimits']['spendControlReached'] = False
        reply['rateLimits']['secondary'] = {'usedPercent': 100, 'resetsAt': NOW + 100}
        self.assertEqual(core.observe_quota(reply, require_both=True), (False, NOW + 100))
        reply['rateLimits']['primary']['usedPercent'] = 101
        with self.assertRaises(ValueError):
            core.observe_quota(reply, require_both=True)

    def test_multibucket_primary_view_is_not_authoritative(self):
        reply = limits()
        reply['rateLimitsByLimitId'] = {'codex': limits(100)['rateLimits']}
        self.assertFalse(core.observe_quota(reply)[0])
    def test_missing_required_bucket_refused(self):
        reply = limits(); reply['rateLimitsByLimitId'] = {'other': limits()['rateLimits']}
        with self.assertRaises(ValueError): core.observe_quota(reply)
    def test_missing_secondary_unknown_for_unattended_send(self):
        with self.assertRaises(ValueError): core.observe_quota({'primary': {'usedPercent': 0}}, require_both=True)
    def test_quota_message_substring_is_not_error_code(self):
        self.assertFalse(core.is_quota_error({'message': 'quoted usageLimitExceeded text'}))


class Transport(unittest.TestCase):
    def test_unverified_desktop_has_no_safe_admission(self):
        self.assertFalse(DesktopBridge().capabilities()['safe_admission'])
    def test_matching_thread_without_admission_never_mutates(self):
        b = DesktopBridge(); calls = []
        def request(method, params=None):
            calls.append(method)
            if method == 'account/rateLimits/read': return limits()
            if method == 'thread/read': return {'thread': {'id': T, 'status': {'type': 'idle'},
                'turns': [{'id': 'failure', 'status': 'failed', 'error': {'codexErrorInfo': 'usageLimitExceeded'}}]}}
            self.fail('unsafe mutation: '+method)
        b.request = request
        with self.assertRaises(BridgeError): b.send_once(T, 'failure')
        self.assertEqual(calls, [])


if __name__ == '__main__': unittest.main()
