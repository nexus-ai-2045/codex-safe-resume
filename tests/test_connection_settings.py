"""接続の明示設定・CLIの固定・設定変更時の監視解除。"""
from contextlib import closing
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import core
from connection_settings import configure, load, validate_executable, prepare, enabled


class SettingsTests(unittest.TestCase):
    def test_windows_manual_exit_window_is_pending_without_attesting_connection(self):
        import json
        config = {'mode': 'managed', 'launch_nonce': 'fixture'}
        state = {'status': 'starting', 'nonce': 'fixture', 'requested_at': 100}
        self.db.execute('INSERT INTO connection_settings VALUES (1,?)', (json.dumps(config),))
        self.db.execute('INSERT INTO desktop_sessions VALUES (1,?)', (json.dumps(state),))
        with patch('connection_settings.sys.platform', 'win32'), \
                patch('desktop_binding.live_binding', return_value=False), \
                patch('time.time', return_value=280):
            pending = load(self.db)
            self.assertEqual(pending['_connection_phase'], 'connecting')
            self.assertFalse(enabled(pending))
        with patch('connection_settings.sys.platform', 'win32'), \
                patch('desktop_binding.live_binding', return_value=False), \
                patch('time.time', return_value=400):
            self.assertEqual(load(self.db)['_connection_phase'], 'failed')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.cli = self.home/'cli'
        self.cli.write_bytes(b'isolated-fixture')
        self.cli.chmod(0o700)
        self.db = core.connection(self.home/'state')
        self.addCleanup(self.db.close)

    def test_unconfigured_is_disabled(self):
        self.assertEqual(load(self.db), {})

    def test_preparation_never_enables_resume_or_claims_desktop_confirmation(self):
        prepare(self.db, self.cli)
        config = load(self.db)
        self.assertEqual(config['mode'], 'prepared')
        self.assertFalse(enabled(config))
        self.assertFalse(config['desktop_confirmed'])
        self.assertFalse(config['manual_exclusion_acknowledged'])
        with self.assertRaises(ValueError): validate_executable(config)
        self.assertEqual(validate_executable(config, require_confirmation=False), self.cli)

    def test_both_acknowledgments_required_without_changing_database(self):
        for desktop, manual in ((False, True), (True, False)):
            with self.assertRaises(ValueError):
                configure(self.db, self.cli, desktop_confirmed=desktop,
                          manual_exclusion_acknowledged=manual)
        self.assertEqual(load(self.db), {})

    def test_changed_executable_is_rejected(self):
        configure(self.db, self.cli, desktop_confirmed=True, manual_exclusion_acknowledged=True)
        config = load(self.db)
        self.assertEqual(validate_executable(config), self.cli)
        self.cli.write_bytes(b'changed-fixture')
        with self.assertRaises(ValueError): validate_executable(config)

    def test_configuration_change_disables_selected_threads(self):
        tid = '123e4567-e89b-12d3-a456-426614174000'
        core.sync(self.db, [dict(id=tid, title='デモ', cwd='', limited=False)])
        core.enable(self.db, tid, True)
        before = core.list_threads(self.db)[0]['generation']
        configure(self.db, self.cli, desktop_confirmed=True, manual_exclusion_acknowledged=True)
        row = core.list_threads(self.db)[0]
        self.assertFalse(row['enabled'])
        self.assertGreater(row['generation'], before)

    def test_changed_connection_requires_reviewed_reset_before_reselection(self):
        tid = '123e4567-e89b-12d3-a456-426614174000'
        core.sync(self.db, [dict(id=tid, title='デモ', cwd='', limited=False)])
        core.enable(self.db, tid, True)
        self.db.execute("UPDATE threads SET reset_at=1800000000,failure_turn='old',"
                        "claimed_at=1799999999,resume_turn='receipt'")
        configure(self.db, self.cli, desktop_confirmed=True, manual_exclusion_acknowledged=True)
        blocked = core.list_threads(self.db)[0]
        self.assertEqual(blocked['state'], 'blocked')
        with self.assertRaises(ValueError):
            core.enable(self.db, tid, True)
        core.mark(self.db, tid, 'idle', expected_generation=blocked['generation'])
        reset = core.list_threads(self.db)[0]
        self.assertEqual(reset['state'], 'idle')
        self.assertFalse(reset['enabled'])
        self.assertEqual(reset['generation'], blocked['generation'] + 1)
        for field in ('reset_at', 'failure_turn', 'claimed_at', 'resume_turn'):
            self.assertIsNone(reset[field])
        self.assertEqual(reset['last_error'], '')
        core.enable(self.db, tid, True)
        self.assertTrue(core.list_threads(self.db)[0]['enabled'])

    def test_relative_and_symlink_executables_rejected(self):
        with self.assertRaises(ValueError):
            configure(self.db, pathlib.Path('relative'), desktop_confirmed=True,
                      manual_exclusion_acknowledged=True)
        link = self.home/'link'
        try: link.symlink_to(self.cli)
        except OSError: self.skipTest('この環境ではsymlink作成不可')
        with self.assertRaises(ValueError):
            configure(self.db, link, desktop_confirmed=True, manual_exclusion_acknowledged=True)

    def test_v2_database_migrates_without_losing_attempts(self):
        self.db.execute("INSERT INTO attempts VALUES ('thread','failure','attempt',1,'sending',NULL)")
        self.db.execute('PRAGMA user_version=2')
        self.db.execute('DROP TABLE connection_settings')
        with closing(core.connection(self.home/'state')) as migrated:
            self.assertEqual(migrated.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(load(migrated), {})
            self.assertEqual(migrated.execute('SELECT attempt_id,state FROM attempts').fetchall(), [('attempt', 'sending')])

    def _seed_managed_session(self):
        import json
        import desktop_binding as binding
        config = dict(mode='managed', executable=str(self.cli), sha256='fixture-hash',
                      launch_nonce='old', manual_exclusion_acknowledged=True,
                      session_home=str(self.home/'state'))
        receipt = dict(nonce='old', status='bound', heartbeat=100,
                       sha256='fixture-hash', initialize_delivered=True,
                       initialized_forwarded=True)
        self.db.execute('INSERT OR REPLACE INTO connection_settings VALUES (1,?)', (json.dumps(config),))
        binding.store(self.db, receipt)
        return config, receipt

    def test_each_setting_replacement_removes_old_managed_proof(self):
        from unittest.mock import patch
        import desktop_binding as binding
        for replacement in ('prepared', 'guarded'):
            with self.subTest(replacement=replacement):
                old_config, _ = self._seed_managed_session()
                with patch('connection_settings._fingerprint', return_value=(self.cli, 'new-hash')):
                    if replacement == 'prepared':
                        prepare(self.db, self.cli)
                    else:
                        configure(self.db, self.cli, desktop_confirmed=True,
                                  manual_exclusion_acknowledged=True)
                self.assertEqual(binding.load(self.db), {})
                self.assertEqual(load(self.db)['mode'], replacement)
                self.assertFalse(binding.update(self.db, 'old', status='bound', heartbeat=101))
                with patch('connection_settings.validate_executable'), \
                        patch.object(binding, 'process_identity', side_effect=AssertionError('no stale OS probe')):
                    self.assertFalse(binding.live_binding(old_config, now=101))

    def test_failed_setting_save_restores_original_session_and_config(self):
        import json
        import sqlite3
        from unittest.mock import patch
        import desktop_binding as binding
        config, receipt = self._seed_managed_session()
        tid = '123e4567-e89b-12d3-a456-426614174000'
        core.sync(self.db, [dict(id=tid, title='fixture', cwd='', limited=False)])
        core.enable(self.db, tid, True)
        before = core.list_threads(self.db)
        self.db.execute("CREATE TEMP TRIGGER reject_settings_write BEFORE UPDATE ON threads "
                        "BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END")
        with patch('connection_settings._fingerprint', return_value=(self.cli, 'new-hash')):
            with self.assertRaises(sqlite3.IntegrityError):
                prepare(self.db, self.cli)
        self.assertEqual(json.loads(self.db.execute('SELECT value FROM connection_settings').fetchone()[0]), config)
        self.assertEqual(binding.load(self.db), receipt)
        self.assertEqual(core.list_threads(self.db), before)
        self.assertFalse(self.db.in_transaction)

    def test_replacement_and_invalidation_are_one_transaction(self):
        import json
        from connection_settings import _store_in_transaction
        import desktop_binding as binding
        config, receipt = self._seed_managed_session()
        with closing(core.connection(self.home/'state', readonly=True)) as observer:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                _store_in_transaction(self.db, dict(mode='prepared'))
                self.assertEqual(binding.load(self.db), {})
                self.assertEqual(binding.load(observer), receipt)
                self.assertEqual(json.loads(observer.execute('SELECT value FROM connection_settings').fetchone()[0]), config)
                self.db.execute('COMMIT')
            finally:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
            self.assertEqual(binding.load(observer), {})
            self.assertEqual(load(observer), dict(mode='prepared'))
