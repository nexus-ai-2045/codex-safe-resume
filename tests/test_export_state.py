"""状態出力はDB・補助ファイルを変更せず、不明と会話ゼロ件を区別する。"""
from contextlib import closing
import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import app
import core


class ExportStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'state'

    def snapshot(self):
        return {
            str(path.relative_to(self.temp.name)): (
                path.stat().st_mode, path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
            for path in Path(self.temp.name).rglob('*')
        }

    def create_database(self, *, journal='DELETE'):
        with closing(core.connection(self.home)) as db:
            db.execute('PRAGMA journal_mode=' + journal)
            db.execute("INSERT INTO threads(id,title,state,enabled,generation,cwd,last_error) "
                       "VALUES(?,?,?,?,?,?,?)",
                       ('12345678-1234-1234-1234-123456789abc', '読取り試験',
                        'waiting', 1, 7, '/private/location', 'private error'))

    def assert_unchanged_export(self):
        before = self.snapshot()
        result = core.export_state(self.home)
        self.assertEqual(before, self.snapshot())
        return result

    def test_missing_directory_is_not_created(self):
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['reason']), ('missing', 'database_missing'))
        self.assertFalse(self.home.exists())
        self.assertEqual(result['conversations'], [])

    def test_missing_database_in_existing_directory_is_not_created(self):
        self.home.mkdir(mode=0o700)
        result = self.assert_unchanged_export()
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(list(self.home.iterdir()), [])

    def test_saved_fields_and_only_saved_fields_are_exported(self):
        self.create_database()
        result = self.assert_unchanged_export()
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['provider'], 'codex')
        self.assertIsNotNone(datetime.datetime.fromisoformat(result['observed_at']).utcoffset())
        self.assertEqual((result['status'], result['reason']), ('ok', None))
        self.assertEqual(result['conversations'], [{
            'id': '12345678-1234-1234-1234-123456789abc', 'title': '読取り試験',
            'state': 'waiting', 'enabled': True, 'generation': 7,
        }])

    def test_empty_compatible_database_is_known_empty(self):
        self.create_database()
        with closing(core.connection(self.home)) as db:
            db.execute('DELETE FROM threads')
            db.execute('PRAGMA journal_mode=DELETE')
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['conversations']), ('ok', []))

    def test_closed_wal_database_fails_without_creating_sidecars(self):
        self.create_database(journal='WAL')
        self.assertEqual([p.name for p in self.home.iterdir()], ['resume.sqlite3'])
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['reason']),
                         ('unknown', 'readonly_snapshot_unavailable'))

    def test_active_wal_database_and_sidecars_are_unchanged(self):
        self.create_database(journal='WAL')
        with closing(core.connection(self.home)) as writer:
            writer.execute("UPDATE threads SET title='未checkpointの現稿'")
            self.assertTrue((self.home / 'resume.sqlite3-wal').exists())
            result = self.assert_unchanged_export()
            self.assertEqual(result['status'], 'unknown')
            self.assertEqual(result['reason'], 'readonly_snapshot_unavailable')
            self.assertEqual(result['conversations'], [])

    def test_legacy_schema_is_not_migrated(self):
        self.home.mkdir(mode=0o700)
        path = self.home / 'resume.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute(core.SCHEMA)
            db.commit()
        path.chmod(0o600)
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['reason']), ('unknown', 'schema_incompatible'))

    def test_future_schema_is_not_migrated(self):
        self.create_database()
        with closing(sqlite3.connect(self.home / 'resume.sqlite3')) as db:
            db.execute('PRAGMA user_version=99')
        result = self.assert_unchanged_export()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['conversations'], [])

    def test_corrupt_database_is_unknown_and_unchanged(self):
        self.home.mkdir(mode=0o700)
        path = self.home / 'resume.sqlite3'
        path.write_bytes(b'not a database')
        path.chmod(0o600)
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['reason']),
                         ('unknown', 'readonly_snapshot_unavailable'))

    def test_real_command_returns_json_without_creating_missing_state(self):
        before = self.snapshot()
        command = subprocess.run(
            [sys.executable, '-B', str(Path(app.__file__).resolve()), 'export-state'],
            env={**os.environ, 'CODEX_RESUME_HOME': str(self.home)},
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)['status'], 'missing')
        self.assertEqual(before, self.snapshot())

    def test_real_command_preserves_unicode_title_with_ascii_stdout(self):
        self.create_database()
        before = self.snapshot()
        command = subprocess.run(
            [sys.executable, '-B', str(Path(app.__file__).resolve()), 'export-state'],
            env={**os.environ, 'CODEX_RESUME_HOME': str(self.home), 'PYTHONIOENCODING': 'ascii'},
            capture_output=True, encoding='ascii', timeout=15, check=False,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        result = json.loads(command.stdout)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['conversations'][0]['title'], '読取り試験')
        self.assertEqual(before, self.snapshot())

    def test_invalid_saved_value_is_not_coerced_to_an_authorization(self):
        self.create_database()
        with closing(sqlite3.connect(self.home / 'resume.sqlite3')) as db:
            db.execute('UPDATE threads SET enabled=2')
            db.commit()
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['reason']), ('unknown', 'invalid_conversation'))

    @unittest.skipUnless(os.name == 'posix', 'POSIXの所有者・権限検査')
    def test_insecure_directory_is_rejected_without_permission_repair(self):
        self.create_database()
        self.home.chmod(0o755)
        result = self.assert_unchanged_export()
        self.assertEqual((result['status'], result['reason']), ('unknown', 'database_unavailable'))

    def test_command_bypasses_discovery_sync_gui_and_external_launches(self):
        self.create_database()
        before = self.snapshot()
        output = io.StringIO()
        with patch.dict(os.environ, {'CODEX_RESUME_HOME': str(self.home)}), \
                patch('sys.argv', ['app.py', 'export-state']), patch('sys.stdout', output), \
                patch('app.discover', side_effect=AssertionError('discover')), \
                patch('app.sync', side_effect=AssertionError('sync')), \
                patch('app.GUI', side_effect=AssertionError('GUI')), \
                patch('subprocess.Popen', side_effect=AssertionError('external launch')), \
                patch('pathlib.Path.mkdir', side_effect=AssertionError('mkdir')):
            self.assertEqual(app.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'ok')
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
