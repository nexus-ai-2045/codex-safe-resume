"""合成SQLiteだけで一覧正本・明示除外・監視対象の独立再照合を検証する。"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import core
import watch


def tid(number):
    return f'123e4567-e89b-12d3-a456-{number:012d}'


def saved(ident):
    return dict(id=ident, title='保存済み', cwd='', limited=True, failure_turn='failure')


class DiscoveryMetadata(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)/'codex'
        self.home.mkdir()
        self.source = sqlite3.connect(self.home/'state_5.sqlite', isolation_level=None)
        self.addCleanup(self.source.close)
        self.source.execute('CREATE TABLE threads(id TEXT, title TEXT, cwd TEXT, name TEXT, '
                            'archived INTEGER, source TEXT, updated_at INTEGER, updated_at_ms INTEGER, '
                            'first_user_message TEXT, has_user_event INTEGER, thread_source TEXT, preview TEXT)')

    def add(self, number, **values):
        record = dict(id=tid(number), title='元タイトル', cwd='', name=None, archived=0,
                      source='cli', updated_at=number, updated_at_ms=None,
                      first_user_message=None, has_user_event=0, thread_source=None, preview='合成プレビュー')
        record.update(values)
        self.source.execute('INSERT INTO threads VALUES (' + ','.join('?' for _ in record) + ')',
                            tuple(record.values()))
        return record['id']

    def discover(self, **kwargs):
        rows, warning = core.discover(self.home, **kwargs)
        self.assertIsNone(warning)
        return rows

    def test_name_precedence_and_source_chronology(self):
        first = self.add(1, name='  明示した 名前  ', updated_at_ms=1800000000123)
        self.add(2, title='後で挿入した古い行', updated_at_ms=1700000000000)
        rows = self.discover(limit=1)
        self.assertEqual([row['id'] for row in rows], [first])
        self.assertEqual(rows[0]['title'], '明示した 名前')
        self.assertAlmostEqual(rows[0]['source_updated_at'], 1800000000.123)

    def test_explicit_name_is_preserved_even_when_it_looks_generic(self):
        self.add(1, name='New Chat', title='別名', first_user_message='本文')
        self.assertEqual(self.discover()[0]['title'], 'New Chat')

    def test_state_first_message_precedes_history_and_is_bounded(self):
        self.add(1, title='New Thread', first_user_message='状態DBの先頭 ' + 'x'*200000)
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as history:
            history.execute('CREATE TABLE thread_items(thread_id TEXT,item_type TEXT,item_json TEXT,rollout_ordinal INTEGER)')
            history.execute('INSERT INTO thread_items VALUES (?,?,?,?)',
                            (tid(1), 'userMessage', json.dumps({'content': [{'text': '履歴の本文'}]}), 1))
            history.commit()
        title = self.discover()[0]['title']
        self.assertTrue(title.startswith('状態DBの先頭'))
        self.assertLessEqual(len(title), 180)

    def test_archive_and_structured_subagents_do_not_consume_candidate_limit(self):
        ordinary = self.add(1)
        for number in range(2, 273):
            self.add(number, archived=number % 2,
                     source=json.dumps({'subAgent': {'parent': 'fixture'}}) if number % 2 == 0 else 'cli')
        self.assertEqual([row['id'] for row in self.discover(limit=1)], [ordinary])

    def test_known_scalar_subagent_variants_are_excluded(self):
        ordinary = self.add(1)
        for number, variant in enumerate(('review', 'compact', 'memory_consolidation'), 2):
            self.add(number, source=json.dumps({'subagent': variant}))
        rows = self.discover(limit=1)
        self.assertEqual([row['id'] for row in rows], [ordinary])

    def test_interactive_defaults_match_official_sources_without_guessing_from_other_columns(self):
        interactive = []
        for number, source in enumerate(('cli', 'vscode', 'atlas', 'chatgpt'), 1):
            interactive.append(self.add(number, source=source, has_user_event=0, thread_source='subagent'))
        excluded = [self.add(10, source='exec'), self.add(11, source='mcp'), self.add(12, source='unknown')]
        self.assertEqual({row['id'] for row in self.discover()}, set(interactive))
        rows = self.discover(include_ids=excluded)
        self.assertTrue(all(row.get('excluded_reason') == 'noninteractive' for row in rows if row['id'] in excluded))

    def test_custom_atlas_and_chatgpt_source_variants_are_interactive(self):
        accepted = [self.add(1, source=json.dumps({'custom': 'atlas'})),
                    self.add(2, source=json.dumps({'custom': 'chatgpt'}))]
        self.add(3, source=json.dumps({'custom': 'another-client'}))
        self.add(4, source=json.dumps({'custom': 'atlas', 'unexpected': True}))
        self.assertEqual({row['id'] for row in self.discover()}, set(accepted))

    def test_source_substrings_do_not_falsely_claim_subagent(self):
        sources = ['subagent', 'ordinary subAgent note', json.dumps('subagent'),
                   json.dumps({'note': 'subagent'}), json.dumps({'nested': {'subagent': {}}}),
                   json.dumps({'subagent': None}), json.dumps({'subagent': 'text'}), '{malformed']
        ids = [self.add(number, source=source) for number, source in enumerate(sources, 1)]
        rows = self.discover(include_ids=ids)
        self.assertEqual(len(rows), len(sources))
        self.assertTrue(all(row.get('excluded_reason') != 'subagent' for row in rows))

    def test_preview_supplies_title_when_name_and_first_message_are_missing(self):
        self.add(1, title='', first_user_message=None, preview='表示用のプレビュー')
        self.assertEqual(self.discover()[0]['title'], '表示用のプレビュー')

    def test_empty_preview_matches_official_normal_list_exclusion(self):
        ordinary = self.add(1)
        empty = self.add(2, name='作成した名前だけ', preview='')
        rows = self.discover(limit=1)
        self.assertEqual([row['id'] for row in rows], [ordinary])
        by_id = {row['id']: row for row in self.discover(include_ids=[empty])}
        self.assertEqual(by_id[empty]['excluded_reason'], 'empty')

    def test_included_ids_recheck_beyond_limit_and_report_only_explicit_exclusions(self):
        old = self.add(1)
        archived = self.add(2, archived=1)
        child = self.add(3, source=json.dumps({'subagent': {}}))
        recent = self.add(4)
        rows = self.discover(limit=1, include_ids=[old, archived, child, tid(999)])
        by_id = {row['id']: row for row in rows}
        self.assertEqual(set(by_id), {old, archived, child, recent})
        self.assertFalse(by_id[old].get('excluded_reason'))
        self.assertEqual(by_id[archived]['excluded_reason'], 'archived')
        self.assertEqual(by_id[child]['excluded_reason'], 'subagent')

    def test_include_ids_use_multiple_bounded_groups_without_omissions(self):
        ids = [self.add(number) for number in range(1, 302)]
        rows = self.discover(limit=1, include_ids=ids + [ids[0]])
        self.assertEqual({row['id'] for row in rows}, set(ids))
        self.assertEqual(len(rows), len(ids))

    def test_legacy_schema_remains_supported(self):
        self.source.execute('DROP TABLE threads')
        self.source.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT)')
        self.source.execute('INSERT INTO threads VALUES (?,?,?)', (tid(1), '既存名', '/fixture'))
        rows = self.discover()
        self.assertEqual(rows[0]['title'], '既存名')
        self.assertNotIn('source_updated_at', rows[0])
        self.assertFalse(rows[0].get('excluded_reason'))

    def test_missing_or_unsupported_inventory_does_not_invent_archive(self):
        self.source.execute('DROP TABLE threads')
        self.source.execute('CREATE TABLE threads(id TEXT)')
        rows, warning = core.discover(self.home, include_ids=[tid(1)])
        self.assertEqual(rows, [])
        self.assertIsNotNone(warning)
        rows, warning = core.discover(self.home/'missing', include_ids=[tid(1)])
        self.assertEqual(rows, [])
        self.assertIsNotNone(warning)
        self.assertFalse((self.home/'missing').exists())

    def target_db(self):
        db = core.connection(Path(self.temp.name)/'state')
        self.addCleanup(db.close)
        core.sync(db, [saved(tid(1))])
        core.enable(db, tid(1), True)
        core.quota(db, tid(1), 'failure', 1800000000)
        db.execute("UPDATE threads SET resume_turn='receipt'")
        db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                   (tid(1), 'failure', 'attempt', 1, 'received', 'receipt'))
        return db

    def test_sync_exclusion_disables_once_without_erasing_attempts_or_evidence(self):
        self.add(1, archived=1)
        db = self.target_db()
        before = core.list_threads(db)[0]
        attempts = [tuple(row) for row in db.execute('SELECT * FROM attempts')]
        rows = self.discover(include_ids=[tid(1)])
        for _ in range(2):
            core.sync(db, rows)
            row = core.list_threads(db)[0]
            self.assertEqual(row['state'], 'blocked')
            self.assertFalse(row['enabled'])
            self.assertIsNone(row['reset_at'])
            self.assertEqual(row['generation'], before['generation'] + 1)
            self.assertEqual(row['failure_turn'], 'failure')
            self.assertEqual(row['resume_turn'], 'receipt')
            self.assertEqual([tuple(record) for record in db.execute('SELECT * FROM attempts')], attempts)

    def test_watch_binds_saved_targets_and_never_snapshots_archived_target(self):
        self.add(1, archived=1)
        self.add(2)
        db = self.target_db()
        probe = Mock()
        probe.capabilities.return_value = {'desktop_binding': True, 'safe_admission': True}
        probe.quota.return_value = {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=probe)
        factory.return_value.__exit__ = Mock(return_value=False)
        def discover_source(**kwargs):
            return core.discover(self.home, limit=1, **kwargs)
        with patch('watch.discover', side_effect=discover_source) as discovery:
            watch.once(db, factory)
        discovery.assert_called_once_with(include_ids=(tid(1),))
        probe.snapshot.assert_not_called()
        probe.send_once.assert_not_called()
        self.assertEqual(next(row for row in core.list_threads(db) if row['id'] == tid(1))['state'], 'blocked')

    def test_active_target_beyond_candidate_limit_is_rechecked_without_false_block(self):
        self.add(1)
        self.add(2)
        with closing(sqlite3.connect(self.home/'thread_history_1.sqlite')) as history:
            history.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT)')
            history.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',
                            (tid(1), 'failure', 'failed', json.dumps({'type': 'usageLimitExceeded'})))
            history.commit()
        db = self.target_db()
        probe = Mock()
        probe.capabilities.return_value = {'desktop_binding': True, 'safe_admission': False}
        probe.snapshot.return_value = dict(id=tid(1), observation_verified=True,
                                          latest_turn='failure', failure_turn='failure',
                                          turn_status='failed', limited=True, awaiting=False)
        probe.quota.return_value = {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=probe)
        factory.return_value.__exit__ = Mock(return_value=False)
        with patch('watch.discover', side_effect=lambda **kwargs: core.discover(self.home, limit=1, **kwargs)) as discovery:
            self.assertEqual(watch.once(db, factory)['state'], 'unsupported-transport')
        discovery.assert_called_once_with(include_ids=(tid(1),))
        probe.snapshot.assert_called_once_with(tid(1))
        probe.send_once.assert_not_called()
        row = next(row for row in core.list_threads(db) if row['id'] == tid(1))
        self.assertEqual(row['state'], 'waiting')
        self.assertTrue(row['enabled'])
        self.assertNotIn('アーカイブ', row['last_error'])

    def test_disabled_monitoring_subagent_is_rechecked_before_live_snapshot(self):
        self.add(1, source=json.dumps({'subagent': {}}))
        self.add(2)
        db = self.target_db()
        db.execute("UPDATE threads SET state='monitoring',enabled=0 WHERE id=?", (tid(1),))
        probe = Mock()
        probe.capabilities.return_value = {'desktop_binding': True, 'safe_admission': True}
        probe.quota.return_value = {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=probe)
        factory.return_value.__exit__ = Mock(return_value=False)
        with patch('watch.discover', side_effect=lambda **kwargs: core.discover(self.home, limit=1, **kwargs)) as discovery:
            watch.once(db, factory)
        discovery.assert_called_once_with(include_ids=(tid(1),))
        probe.snapshot.assert_not_called()
        probe.send_once.assert_not_called()
        row = next(row for row in core.list_threads(db) if row['id'] == tid(1))
        self.assertEqual(row['state'], 'blocked')
        self.assertEqual(row['resume_turn'], 'receipt')

    def test_watch_warning_does_not_label_target_as_archived(self):
        db = self.target_db()
        before = core.list_threads(db)[0]
        factory = Mock()
        with patch('watch.discover', return_value=([], '合成した読取り失敗')):
            self.assertEqual(watch.once(db, factory)['state'], 'unavailable')
        self.assertEqual(core.list_threads(db)[0], before)
        factory.assert_not_called()
