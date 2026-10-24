"""状態が戻る・切り替わる境界で、表示用の受領と操作を取り残さない。"""
from contextlib import closing
import json
from pathlib import Path
import queue
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

import app
import auto_watch
import core
import watch
from tests import test_resume_list as fixtures
from tests.test_auto_watch import Bridge, item, NOW, T
from tests.test_usage_ui import sample


class DisplayTransitions(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResumeList()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gui = self.fixture.gui
        self.gui.closing = False
        self.gui.simple_mode = False
        self.gui.auto_status = Mock()
        self.gui.auto_button = Mock()
        self.gui.connection_action = Mock()
        self.gui.connection_note = Mock()
        self.gui.plan_note = Mock()
        self.gui.usage_status = Mock()
        self.gui.quota_cards = Mock()
        self.gui.usage_updated = Mock()
        self.gui.usage_running = False
        self.gui.usage_queue = queue.Queue(maxsize=1)
        self.gui.monitor_queue = queue.Queue()
        self.gui.monitor_running = False
        auto_watch.initialize(self.gui.db)
        auto_watch.set_enabled(self.gui.db, True)
        self.config = dict(mode='managed', _runtime_attested=True, _connection_phase='ready')
        loader = patch('app.connection_options', side_effect=lambda db: dict(self.config))
        self.loader = loader.start()
        self.addCleanup(loader.stop)

    def receipt(self, state='checked', results=(), age=0):
        value = dict(state=state, results=list(results),
                     _ui_policy=auto_watch.status(self.gui.db), _ui_config=dict(self.config),
                     _ui_revision=getattr(self.gui, 'monitor_display_revision', 0),
                     _ui_context_revision=getattr(self.gui, 'monitor_context_revision', 0),
                     _ui_rows=app.monitor_rows_snapshot(core.list_threads(self.gui.db)),
                     _ui_finished_at=time.monotonic() - age)
        self.gui.last_monitor_result = value
        self.gui.last_monitor_policy = value['_ui_policy']
        self.gui.last_monitor_config = value['_ui_config']
        self.gui.last_monitor_at = value['_ui_finished_at']
        return value

    def status(self):
        return self.gui.auto_status.set.call_args.args[0]

    def reading(self, remaining=75):
        value = sample()
        value['checked_at'] = time.time()
        value['windows'][0]['remaining_percent'] = remaining
        value.update(plan_type='plus', desktop_connection='read', peer_status='read',
                     account_match=True, peer_limits=dict(value, plan_type='plus'))
        return value

    def deliver_usage(self, value):
        self.gui.usage_queue.put((json.dumps(self.config, sort_keys=True), value))
        self.gui.usage_running = True
        self.gui.drain_usage()

    def test_受領の要確認は現在表示できる要確認行だけを参照する(self):
        for state, known, exists in (
                ('idle', [{'id': '対象'}], True),
                ('completed', [{'id': '対象'}], True),
                ('blocked', [], True),
                ('blocked', [{'id': '対象', 'excluded_reason': 'archived'}], True),
                ('blocked', [{'id': '対象'}], False)):
            with self.subTest(state=state, known=known, exists=exists):
                self.gui.rows = [{'id': '対象', 'state': state}] if exists else []
                self.gui.known = known
                self.receipt(results=[('対象', 'blocked')])
                self.gui.refresh_auto_status()
                self.assertNotIn('要確認', self.status())
                self.gui.connection_action.grid_remove.assert_called()

    def test_選び直し直後に受領と要確認の操作が失効する(self):
        tid = self.fixture.add(1, '確認するチャット', state='blocked', limited=True)
        self.gui.selected.return_value = tid
        self.receipt(results=[(tid, 'blocked')])
        self.gui.refresh_auto_status()
        self.assertIn('要確認', self.status())
        attempts = [tuple(row) for row in self.gui.db.execute('SELECT * FROM attempts')]
        self.gui.review_reset()
        self.assertNotIn('要確認', self.status())
        self.assertEqual(self.gui.last_monitor_result['results'], [])
        self.gui.connection_action.grid_remove.assert_called()
        self.assertFalse(self.gui.rows[0]['enabled'])
        self.assertEqual([tuple(row) for row in self.gui.db.execute('SELECT * FROM attempts')], attempts)

    def test_詳細表示でも要確認化と完了を直ちに再計算する(self):
        tid = self.fixture.add(1, '状態を確認するチャット')
        self.gui.selected.return_value = tid
        self.receipt()
        self.gui.render_rows()
        self.assertIn('監視中', self.status())
        self.gui.finish('blocked')
        self.assertIn('要確認', self.status())
        action = self.gui.connection_action.configure.call_args.kwargs
        self.assertEqual(action['text'], '要確認のチャットを表示')
        self.assertEqual(action['command'], self.gui.show_review_targets)
        self.gui.finish('completed')
        self.assertNotIn('要確認', self.status())
        self.gui.connection_action.grid_remove.assert_called()

    def test_個別対象の選択と解除で以前の受領を失効させる(self):
        tid = self.fixture.add(1, '選択するチャット', limited=True)
        self.gui.selected.return_value = tid
        self.gui.target_included = Mock()
        for included in (True, False):
            with self.subTest(included=included):
                self.receipt()
                self.gui.target_included.get.return_value = included
                self.gui.change_target()
                self.assertIsNone(self.gui.last_monitor_at)
                self.assertNotIn('監視中', self.status())
                self.assertEqual(bool(self.gui.rows[0]['auto_excluded']), not included)

    def test_全体の停止と再開で行の操作表示も読み直す(self):
        tid = self.fixture.add(1, '送信処理を確認するチャット', state='claimed', enabled=True)
        self.gui.request_usage = Mock()
        self.gui.toggle_auto_watch()
        self.assertIn('停止中', self.status())
        self.assertEqual(self.gui.rows[0]['state'], 'blocked')
        self.assertFalse(self.gui.rows[0]['enabled'])
        self.assertEqual(self.gui.tree.insert.call_args.kwargs['values'][3], '要確認')
        with patch('app.connection_enabled', return_value=True):
            self.gui.toggle_auto_watch()
        self.assertIn('要確認', self.status())
        self.assertEqual(self.gui.rows[0]['id'], tid)

    def test_削除と検出消失と除外理由の着脱で受領を取り残さない(self):
        tid = self.fixture.add(1, '表示が切り替わるチャット', state='blocked')
        observed = dict(self.gui.known[0])
        for known in ([], [dict(observed, excluded_reason='archived')], [observed]):
            with self.subTest(known=known):
                self.receipt(results=[(tid, 'blocked')])
                with patch('app.discover', return_value=(known, None)):
                    self.gui.refresh()
                self.assertEqual(self.gui.last_monitor_result['results'], [])
                self.assertEqual('要確認' in self.status(), bool(known and not known[0].get('excluded_reason')))
        self.receipt(results=[(tid, 'blocked')])
        self.gui.db.execute('DELETE FROM threads WHERE id=?', (tid,))
        self.gui.reload_rows()
        self.gui.render_rows()
        self.assertNotIn('要確認', self.status())
        self.assertEqual(self.gui.last_monitor_result['results'], [])

    def test_監視エラーの期限切れで文言と修復操作を同時に更新する(self):
        for state in ('checked', 'quota-wait', 'offline', 'unavailable',
                      'unsupported-transport', 'monitor-error'):
            with self.subTest(state=state):
                self.receipt(state)
                self.gui.refresh_auto_status()
                before = self.gui.db.total_changes
                self.gui.last_monitor_at -= 91
                self.loader.reset_mock()
                self.gui.connection_action.grid_remove.reset_mock()
                with patch('app.connection_enabled') as attest, patch('app.monitor_once') as sender:
                    self.gui.display_tick()
                self.assertIn('接続確認中', self.status())
                self.gui.connection_action.grid_remove.assert_called_once()
                self.loader.assert_not_called()
                attest.assert_not_called()
                sender.assert_not_called()
                self.assertEqual(self.gui.db.total_changes, before)

    def test_監視期限切れでも保存済みの要確認行は保持する(self):
        self.fixture.add(1, '確認が終わっていないチャット', state='blocked')
        self.receipt(age=91)
        self.gui.refresh_auto_status()
        self.gui.display_tick()
        self.assertIn('要確認', self.status())
        self.gui.connection_action.grid.assert_called()

    def test_遅れて届いた監視結果の期限を受取り時に延長しない(self):
        result = self.receipt('offline', age=91)
        self.gui.monitor_queue.put(result)
        self.gui.drain_monitor()
        self.assertIn('接続確認中', self.status())
        self.assertEqual(self.gui.last_monitor_at, result['_ui_finished_at'])
        self.assertFalse(self.gui.monitor_running)

    def test_行の変更前に始めた監視結果を後から採用しない(self):
        tid = self.fixture.add(1, '選び直すチャット', state='blocked')
        result = self.receipt('offline', results=[(tid, 'blocked')])
        self.gui.selected.return_value = tid
        self.gui.review_reset()
        self.gui.monitor_queue.put(result)
        self.gui.drain_monitor()
        self.assertNotIn('要確認', self.status())
        self.assertIsNone(self.gui.last_monitor_at)
        self.assertFalse(self.gui.monitor_running)

    def test_行が一致しても明示的に失効した受領を再採用しない(self):
        tid = self.fixture.add(1, '表示の根拠が変わるチャット')
        for ids in (None, {tid}):
            with self.subTest(ids=ids):
                result = self.receipt('offline')
                self.gui.invalidate_monitor_results(ids)
                self.gui.monitor_queue.put(result)
                self.gui.drain_monitor()
                self.assertIsNone(self.gui.last_monitor_at)
                self.assertIsNot(self.gui.last_monitor_result, result)
                self.assertIn('接続確認中', self.status())

    def test_監視自身が登録と待機と送信と完了を行った周期の受領を採用する(self):
        bridge = Bridge()
        waiting = {'rateLimits': {
            'primary': {'usedPercent': 100, 'resetsAt': NOW + 60},
            'secondary': {'usedPercent': 0, 'resetsAt': NOW + 60}}}
        available = {'rateLimits': {
            'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
        bridge.quota = Mock(return_value=waiting)
        database = Path(self.gui.db.execute('PRAGMA database_list').fetchone()[2])
        with patch('app.connection', side_effect=lambda: closing(core.connection(database.parent))), \
                patch('app.monitor_once', side_effect=lambda db: watch.once(db, lambda: bridge)), \
                patch('watch.discover', side_effect=lambda **kwargs: ([bridge.current], None)), \
                patch('core.time.time', return_value=NOW) as clock, \
                patch('app.threading.Thread') as worker:
            worker.side_effect = lambda *, target, daemon, **kwargs: Mock(start=target)
            for phase, expected_state in (('登録と待機', 'waiting'), ('送信', 'monitoring'),
                                          ('完了', 'completed')):
                with self.subTest(phase=phase):
                    if phase == '送信':
                        bridge.quota.return_value = available
                        clock.return_value = NOW + 61
                    elif phase == '完了':
                        bridge.current = item('resume-failure', limited=False, failure_turn=None,
                                              turn_status='completed')
                    self.gui.known = [dict(bridge.current)]
                    self.gui.monitor_tick()
                    result = self.gui.monitor_queue.get_nowait()
                    self.assertEqual(result['_ui_rows'],
                                     app.monitor_rows_snapshot(core.list_threads(self.gui.db)))
                    revision = result['_ui_revision']
                    if phase == '送信':
                        # 定期再読込みが監視自身の遷移を先に表示しても、受領を捨てない。
                        self.gui.reload_rows()
                    self.gui.monitor_queue.put(result)
                    self.gui.drain_monitor()
                    self.assertEqual(self.gui.rows[0]['state'], expected_state)
                    self.assertIs(self.gui.last_monitor_result, result)
                    self.assertGreater(self.gui.monitor_display_revision, revision)
                    self.assertEqual(result['_ui_revision'], self.gui.monitor_display_revision)
                    self.assertTrue(self.status().startswith('監視中'))
                    self.assertFalse(self.gui.monitor_running)
        self.assertEqual(bridge.sent, [(T, 'failure')])
        self.assertEqual(self.gui.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 1)

    def test_受領待ちの間に外部で行が変わったら古い受領全体を採用しない(self):
        tid = self.fixture.add(1, '外部で変更するチャット', state='blocked')
        database = self.gui.db.execute('PRAGMA database_list').fetchone()[2]
        for state in ('checked', 'quota-wait', 'offline', 'monitor-error'):
            for change in ("state='idle',enabled=0", 'generation=generation+1',
                           'enabled=1', 'auto_excluded=1', "failure_turn='別の停止'",
                           "resume_turn='別の再開'", 'reset_at=1900000000'):
                with self.subTest(state=state, change=change):
                    initial = 'blocked' if change.startswith('state=') else 'idle'
                    self.gui.db.execute('''UPDATE threads SET state=?,enabled=0,auto_excluded=0,
                                          failure_turn=NULL,resume_turn=NULL,reset_at=NULL WHERE id=?''',
                                        (initial, tid))
                    self.gui.reload_rows()
                    result = self.receipt(state, results=[(tid, 'blocked')])
                    # 受領待ちの間の外部変更を、受取時の読み直しで初めて検知する。
                    with closing(sqlite3.connect(database, isolation_level=None)) as other:
                        other.execute(f'UPDATE threads SET {change} WHERE id=?', (tid,))
                    revision = result['_ui_revision']
                    self.gui.monitor_running = True
                    self.gui.monitor_queue.put(result)
                    self.gui.drain_monitor()
                    self.assertGreater(self.gui.monitor_display_revision, revision)
                    self.assertNotEqual(self.gui.last_monitor_result, result)
                    self.assertEqual(self.gui.last_monitor_result['results'], [])
                    self.assertIsNone(self.gui.last_monitor_at)
                    self.assertIn('接続確認中', self.status())
                    self.assertNotIn('要確認', self.status())
                    self.assertNotIn('監視中', self.status())
                    self.assertFalse(self.gui.monitor_running)

    def test_接続の切替を検知した時点で利用枠とプランを消す(self):
        for changes in ({'_runtime_attested': False, '_connection_phase': 'connecting'},
                        {'_runtime_attested': False, '_connection_phase': 'failed'},
                        {'launch_nonce': '別の接続'}):
            with self.subTest(changes=changes):
                self.gui.update_connection_note(self.reading())
                self.gui.render_usage()
                self.receipt()
                self.config.update(changes)
                self.gui.refresh_auto_status()
                self.assertIsNone(self.gui.last_usage_result)
                self.assertIsNone(self.gui.desktop_read_state)
                self.assertIn('未確認', self.gui.plan_note.set.call_args.args[0])
                self.assertIsNone(self.gui.last_monitor_at)
                self.assertTrue(all(card['remaining_percent'] is None
                                    for card in self.gui.quota_cards.render.call_args.args[0]['cards']))

    def test_未対応の接続要求は対象や取得済みの状態を変更しない(self):
        self.fixture.add(1, '確認済みのチャット', state='waiting', enabled=True, limited=True)
        self.gui.update_connection_note(self.reading())
        before = self.gui.last_usage_result
        with patch('app.sys.platform', 'darwin'), patch('app.start_auto_connection') as start, patch(
                'app.prepare_queue_connection', side_effect=ValueError('unavailable')), patch(
                'app.messagebox.showinfo') as notice:
            self.gui.reconnect_auto_watch()
            self.gui.queue_setup_thread.join(2)
            self.assertFalse(self.gui.queue_setup_thread.is_alive())
            self.gui.drain_queue_setup()
        start.assert_not_called()
        self.assertIn('接続を確認できません', notice.call_args.args[1])
        self.assertIs(self.gui.last_usage_result, before)
        self.assertEqual(self.gui.db.execute('SELECT state,enabled FROM threads').fetchone()[:], ('waiting', 1))

    def test_接続の修復中から失敗と成功への切替で操作と再取得を更新する(self):
        self.config.update(_runtime_attested=False, _connection_phase='connecting')
        self.gui.refresh_auto_status()
        self.gui.request_usage = Mock()
        self.config['_connection_phase'] = 'failed'
        self.gui.poll_desktop_connection()
        self.assertIn('要確認', self.status())
        action = self.gui.connection_action.configure.call_args.kwargs
        self.assertEqual(action['text'], '接続の確認・修復…')
        self.assertEqual(action['command'], self.gui.reconnect_auto_watch)
        self.config.update(_runtime_attested=True, _connection_phase='ready')
        self.gui.poll_desktop_connection()
        self.assertIn('接続確認中', self.status())
        self.gui.connection_action.grid_remove.assert_called()
        self.gui.request_usage.assert_called_once()

    def test_利用枠の取得開始失敗で前回成功を現在の接続として残さない(self):
        self.gui.update_connection_note(self.reading())
        with patch('app.threading.Thread') as worker:
            worker.return_value.start.side_effect = RuntimeError('開始失敗')
            self.gui.request_usage()
        self.assertFalse(self.gui.usage_running)
        self.assertTrue(self.gui.usage_failed)
        self.assertIsNone(self.gui.desktop_read_state)
        self.assertIn('前回取得', self.gui.plan_note.set.call_args.args[0])
        self.assertNotIn('読取り成功', self.gui.connection_note.set.call_args.args[0])
        self.assertTrue(self.gui.quota_cards.render.call_args.args[0]['stale'])

    def test_利用枠の成功失敗成功で履歴表示とプランと共有接続を更新する(self):
        self.deliver_usage(self.reading())
        self.assertEqual(self.gui.desktop_read_state, 'read')
        self.deliver_usage(None)
        self.assertTrue(self.gui.usage_failed)
        self.assertIsNone(self.gui.desktop_read_state)
        result = self.reading(remaining=20)
        result.update(desktop_connection='unavailable', peer_status='not_provided',
                      peer_limits=None, plan_type='pro', account_match=None)
        self.deliver_usage(result)
        self.assertFalse(self.gui.usage_failed)
        self.assertEqual(self.gui.desktop_read_state, 'unavailable')
        self.assertNotIn('前回取得', self.gui.plan_note.set.call_args.args[0])
        self.assertNotIn('読取り成功', self.gui.connection_note.set.call_args.args[0])
        view = self.gui.quota_cards.render.call_args.args[0]
        self.assertFalse(view['stale'])
        self.assertEqual(view['source'], '公式CLI')
        self.assertEqual(view['cards'][1]['remaining_percent'], 20)

    def test_利用枠の期限切れでプランと共有接続も履歴表示になる(self):
        self.gui.update_connection_note(self.reading())
        reading = self.gui.last_usage_result
        reading['checked_at'] -= 121
        reading['peer_limits']['checked_at'] -= 121
        self.loader.reset_mock()
        self.gui.display_tick()
        self.assertIn('前回取得', self.gui.plan_note.set.call_args.args[0])
        self.assertNotIn('読取り成功', self.gui.connection_note.set.call_args.args[0])
        self.assertIsNone(self.gui.desktop_read_state)
        self.loader.assert_not_called()
        self.assertTrue(self.gui.quota_cards.render.call_args.args[0]['stale'])

    def test_設定不一致の利用枠受領を破棄した後の毎秒表示も未取得を保つ(self):
        self.gui.update_connection_note(self.reading())
        old = json.dumps(self.config, sort_keys=True)
        self.gui.usage_queue.put((old, self.reading()))
        self.gui.usage_running = True
        self.config['launch_nonce'] = '新しい接続'
        self.gui.request_usage = Mock()
        self.gui.drain_usage()
        self.gui.display_tick()
        self.assertIsNone(self.gui.last_usage_result)
        self.assertNotIn('75%', self.gui.usage_status.set.call_args.args[0])
        self.assertIn('未取得', self.gui.usage_updated.set.call_args.args[0])
        self.gui.request_usage.assert_called_once()

    def test_利用枠受領の照合と表示は同じ接続設定を使う(self):
        previous = dict(self.config)
        following = dict(self.config, launch_nonce='次の接続')
        self.loader.side_effect = [previous, following]
        value = self.reading()
        self.deliver_usage(value)
        self.loader.assert_called_once()
        self.assertEqual(self.gui.display_auto_config, previous)
        self.assertIs(self.gui.last_usage_result, value)
        self.gui.refresh_auto_status()
        self.assertEqual(self.gui.display_auto_config, following)
        self.assertIsNone(self.gui.last_usage_result)

    def test_表示フィルタと検索で選択行が消えたら詳細と対象操作も解除する(self):
        tid = self.fixture.add(1, '選択するチャット', state='waiting', limited=True)
        blocked = self.fixture.add(2, '確認するチャット', state='blocked')
        idle = self.fixture.add(3, '待機するチャット')
        items, selection = {}, []
        self.gui.tree.get_children.side_effect = lambda: tuple(items)
        def delete(tid):
            items.pop(tid)
            if tid in selection:
                selection.clear()
        self.gui.tree.delete.side_effect = delete
        self.gui.tree.insert.side_effect = lambda *args, **kwargs: items.update({kwargs['iid']: kwargs})
        self.gui.tree.exists.side_effect = lambda tid: tid in items
        self.gui.tree.selection_set.side_effect = lambda tid: selection.__setitem__(slice(None), [tid])
        self.gui.tree.selection.side_effect = lambda: tuple(selection)
        self.gui.selected = app.GUI.selected.__get__(self.gui)
        self.gui.select_row = app.GUI.select_row.__get__(self.gui)
        self.gui.details = Mock()
        self.gui.target_toggle = Mock()
        self.gui.target_included = Mock()
        self.gui.target_reason = Mock()
        self.gui.view_filter = Mock()
        for variable, value in ((self.gui.view_filter, 'すべて'),
                                (self.gui.filter, 'すべて'), (self.gui.search, '')):
            variable.get.return_value = value
        for view, expected in (('すべて', {tid, blocked, idle}), ('確認が必要', {blocked}),
                               ('解除待ち・再開中', {tid})):
            self.gui.view_filter.get.return_value = view
            self.gui.render_rows()
            self.assertEqual(set(items), expected)
        for field, value in (('view_filter', '確認が必要'), ('search', '一致しない検索'),
                             ('filter', '完了')):
            with self.subTest(field=field):
                self.gui.view_filter.get.return_value = 'すべて'
                self.gui.filter.get.return_value = 'すべて'
                self.gui.search.get.return_value = ''
                self.gui.render_rows()
                self.gui.tree.selection_set(tid)
                self.gui.select_row()
                self.gui.target_toggle.state.assert_called_with(['!disabled'])
                getattr(self.gui, field).get.return_value = value
                self.gui.render_rows()
                self.assertIsNone(self.gui.selected())
                self.gui.target_toggle.state.assert_called_with(['disabled'])
                self.gui.target_included.set.assert_called_with(False)
                self.assertNotIn('選択するチャット', self.gui.details.set.call_args.args[0])

    def test_毎秒表示の証明キャッシュを送信許可へ流用しない(self):
        self.gui.refresh_auto_status()
        self.assertTrue(self.gui.display_auto_ready)
        auto_watch.set_enabled(self.gui.db, False)
        self.gui.request_desktop_connection = Mock(return_value=False)
        with patch('app.connection_enabled', return_value=False) as attest:
            self.gui.toggle_auto_watch()
        attest.assert_called_once()
        self.gui.request_desktop_connection.assert_called_once()
        self.assertFalse(auto_watch.status(self.gui.db)['enabled'])


if __name__ == '__main__':
    unittest.main()
