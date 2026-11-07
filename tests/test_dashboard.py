"""通常画面の状態表示と個別対象指定は送信操作から独立する。"""
import time
import json
import unittest
from unittest.mock import Mock, patch
from tests import test_resume_list as fixtures
import app
import auto_watch
import core
import watch


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResumeList()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.gui = self.fixture.gui
        self.gui.target_included = Mock()
        self.gui.target_toggle = Mock()
        self.gui.target_reason = Mock()
        self.gui.details = Mock()

    def test_selecting_row_only_updates_target_controls(self):
        tid = self.fixture.add(1, '対象候補', limited=True)
        self.gui.selected.return_value = tid
        before = self.gui.db.total_changes
        app.GUI.select_row(self.gui)
        self.assertEqual(self.gui.db.total_changes, before)
        self.gui.target_included.set.assert_called_with(True)
        self.gui.target_toggle.state.assert_called_with(['!disabled'])

    def test_対象チェックは有効状態によらず保存済みの個別除外を参照する(self):
        for state in ('idle', 'waiting', 'monitoring', 'blocked', 'completed'):
            for enabled in (0, 1):
                for excluded in (0, 1):
                    with self.subTest(state=state, enabled=enabled, excluded=excluded):
                        before = self.gui.db.total_changes
                        self.gui.update_target_controls(dict(
                            state=state, enabled=enabled, auto_excluded=excluded))
                        self.gui.target_included.set.assert_called_with(not excluded)
                        self.assertEqual(self.gui.db.total_changes, before)
        self.gui.update_target_controls(None)
        self.gui.target_included.set.assert_called_with(False)
        self.gui.target_toggle.state.assert_called_with(['disabled'])

    def test_全体停止でも保存済み対象は選択済みで一回押すと除外する(self):
        tid = self.fixture.add(1, '停止中も対象指定を保持するチャット', limited=True)
        auto_watch.initialize(self.gui.db)
        auto_watch.set_enabled(self.gui.db, True)
        core.enable(self.gui.db, tid, True)
        self.gui.selected.return_value = tid
        self.gui.select_row.side_effect = lambda: app.GUI.select_row(self.gui)
        self.gui.target_included.set.side_effect = lambda value: setattr(
            self.gui.target_included.get, 'return_value', value)

        with patch('app.enable', wraps=core.enable) as enable, \
                patch('app.monitor_once') as sender, patch('app.start_auto_connection') as connect:
            self.gui.toggle_auto_watch()
            self.assertFalse(auto_watch.status(self.gui.db)['enabled'])
            self.assertEqual((self.gui.rows[0]['enabled'], self.gui.rows[0]['auto_excluded']), (0, 0))
            self.assertEqual(self.gui.tree.insert.call_args.kwargs['values'][3], '対象')
            self.assertTrue(self.gui.target_included.get())
            self.gui.target_toggle.state.assert_called_with(['!disabled'])

            # チェックを一回押すと表示値が反転してから変更処理が呼ばれる。
            self.gui.target_included.set(not self.gui.target_included.get())
            self.gui.change_target()
            enable.assert_called_once_with(self.gui.db, tid, False)
            self.assertEqual((self.gui.rows[0]['enabled'], self.gui.rows[0]['auto_excluded']), (0, 1))
            self.assertEqual(self.gui.tree.insert.call_args.kwargs['values'][3], '除外')
            self.assertFalse(self.gui.target_included.get())
            self.assertFalse(auto_watch.status(self.gui.db)['enabled'])
            sender.assert_not_called()
            connect.assert_not_called()

    def test_監視で停止した対象は選び直し後もチェックを入れるまで除外する(self):
        tid = self.fixture.add(1, '選び直す対象', limited=True, enabled=True)
        auto_watch.initialize(self.gui.db)
        auto_watch.set_enabled(self.gui.db, True)
        core.enable(self.gui.db, tid, True)
        watch.block(self.gui.db, tid, '状態の確認が必要です')
        self.gui.selected.return_value = tid
        self.gui.reload_rows()
        app.GUI.select_row(self.gui)
        self.gui.target_included.set.assert_called_with(True)
        self.gui.target_toggle.state.assert_called_with(['disabled'])
        self.assertFalse(self.gui.rows[0]['auto_excluded'])
        attempts = [tuple(row) for row in self.gui.db.execute('SELECT * FROM attempts')]
        self.fixture.dialogs.askyesno.return_value = True
        self.gui.review_reset()
        row = self.gui.rows[0]
        self.assertEqual(row['state'], 'idle')
        self.assertEqual((row['enabled'], row['auto_excluded']), (0, 1))
        app.GUI.select_row(self.gui)
        self.gui.target_included.set.assert_called_with(False)
        self.gui.target_toggle.state.assert_called_with(['!disabled'])
        self.assertFalse(auto_watch.eligible(self.gui.db, self.gui.known[0]))
        self.assertEqual([tuple(row) for row in self.gui.db.execute('SELECT * FROM attempts')], attempts)
        self.gui.target_included.get.return_value = True
        self.gui.change_target()
        app.GUI.select_row(self.gui)
        self.gui.target_included.set.assert_called_with(True)
        self.assertEqual((self.gui.rows[0]['enabled'], self.gui.rows[0]['auto_excluded']), (1, 0))
        self.assertTrue(auto_watch.eligible(self.gui.db, self.gui.known[0]))

    def test_確認中に世代が変わった選び直しは個別除外も変更しない(self):
        with patch('core.time.time', return_value=1792613512.999) as clock:
            self.check_review_generation_change(clock, 1792613512.999)

    def test_選び直しの確認と再読込みが秒境界をまたいでも状態と履歴を保つ(self):
        with patch('core.time.time', return_value=1792613512.999) as clock:
            self.check_review_generation_change(clock, 1792613513.0)

    def check_review_generation_change(self, clock, refresh_time):
        tid = self.fixture.add(1, '確認中に変わる対象', limited=True, enabled=True)
        watch.block(self.gui.db, tid, '状態の確認が必要です')
        self.gui.selected.return_value = tid
        attempts = [tuple(row) for row in self.gui.db.execute('SELECT * FROM attempts')]
        expected = None
        def change_generation(*args, **kwargs):
            nonlocal expected
            self.gui.db.execute('UPDATE threads SET generation=generation+1 WHERE id=?', (tid,))
            expected = core.list_threads(self.gui.db)
            clock.return_value = refresh_time
            return True
        self.fixture.dialogs.askyesno.side_effect = change_generation
        self.gui.review_reset()
        self.fixture.dialogs.showwarning.assert_called_once()
        actual = core.list_threads(self.gui.db)
        # 再読込みの観測時刻は更新される。選び直しが変えてはいけない項目を全件照合する。
        def selection_state(rows):
            return {row['id']: {key: value for key, value in row.items() if key != 'updated_at'}
                    for row in rows}
        self.assertEqual(selection_state(actual), selection_state(expected))
        self.assertEqual(expected[0]['updated_at'], 1792613512)
        self.assertEqual(actual[0]['updated_at'], int(refresh_time))
        self.assertEqual((self.gui.rows[0]['enabled'], self.gui.rows[0]['auto_excluded']), (0, 0))
        self.assertEqual([tuple(row) for row in self.gui.db.execute('SELECT * FROM attempts')], attempts)

    def test_target_selection_during_global_off_never_connects_or_sends(self):
        tid = self.fixture.add(1, '候補', limited=True)
        auto_watch.initialize(self.gui.db)
        core.enable(self.gui.db, tid, False)
        self.gui.selected.return_value = tid
        self.gui.target_included.get.return_value = True
        with patch('app.monitor_once') as sender, patch('app.start_auto_connection') as connect:
            self.gui.change_target()
        sender.assert_not_called(); connect.assert_not_called()
        row = next(r for r in core.list_threads(self.gui.db) if r['id'] == tid)
        self.assertEqual(row['auto_excluded'], 0)
        self.assertFalse(auto_watch.status(self.gui.db)['enabled'])
        self.assertEqual(watch.once(self.gui.db, lambda: self.fail('connected'))['state'], 'disabled')

    def test_stopped_and_inflight_rows_cannot_be_reenabled_by_checkbox(self):
        for index, state in enumerate(('blocked','completed','claimed','dispatched'), 1):
            tid = self.fixture.add(index, state, limited=True, state=state)
            self.gui.selected.return_value = tid
            self.gui.target_included.get.return_value = True
            before = [tuple(r) for r in self.gui.db.execute('SELECT * FROM attempts')]
            self.gui.change_target()
            row = next(r for r in core.list_threads(self.gui.db) if r['id'] == tid)
            self.assertEqual(row['state'], state)
            self.assertFalse(row['enabled'])
            self.assertEqual([tuple(r)for r in self.gui.db.execute('SELECT * FROM attempts')],before)

    def test_all_shows_running_chat_and_pending_filter_keeps_only_resume_work(self):
        stopped = self.fixture.add(1, '停止', limited=True, state='waiting')
        running = self.fixture.add(2, '作業中', turn_status='inProgress')
        self.gui.simple_mode = True
        self.gui.view_filter = Mock(); self.gui.view_filter.get.return_value = 'すべて'
        self.gui.render_rows(); self.assertEqual(set(self.fixture.order()),{stopped,running})
        self.gui.tree.insert.reset_mock(); self.gui.view_filter.get.return_value = '解除待ち・再開中'
        self.gui.render_rows(); self.assertEqual(self.fixture.order(), [stopped])

    def test_monitoring_label_requires_matching_fresh_monitor_receipt(self):
        gui = self.gui
        gui.auto_status = Mock(); gui.auto_button = Mock()
        policy={'enabled': True, 'generation': 2}; config={'mode':'managed'}
        with patch('app.auto_watch_status',return_value=policy), patch('app.connection_options',return_value=config), patch('app.connection_enabled',return_value=True):
            gui.refresh_auto_status = lambda:app.GUI.refresh_auto_status(gui)
            gui.refresh_auto_status(); self.assertNotIn('監視中',gui.auto_status.set.call_args.args[0])
            gui.last_monitor_result={'state':'checked','results':[]}
            gui.last_monitor_policy=policy; gui.last_monitor_config=config; gui.last_monitor_at=time.monotonic()
            gui.refresh_auto_status();self.assertIn('監視中',gui.auto_status.set.call_args.args[0])
            gui.last_monitor_policy={'enabled':True,'generation':1}
            gui.refresh_auto_status();self.assertNotIn('監視中',gui.auto_status.set.call_args.args[0])

    def test_fresh_loaded_attestation_is_used_once_for_display(self):
        auto_watch.initialize(self.gui.db)
        auto_watch.set_enabled(self.gui.db, True)
        self.gui.db.execute('INSERT INTO connection_settings VALUES (1, ?)',
                            (json.dumps({'mode': 'managed'}),))
        self.gui.auto_status = Mock()
        with patch('desktop_binding.live_binding', return_value=True) as attest:
            app.GUI.refresh_auto_status(self.gui)
        self.assertEqual(attest.call_count, 1)

    def test_historical_without_any_success_does_not_claim_previous_plan(self):
        self.gui.connection_note, self.gui.plan_note = Mock(), Mock()
        self.gui.last_usage_result = None
        self.gui.refresh_auto_status = Mock()
        app.GUI.update_connection_note(self.gui, historical=True)
        self.assertIn('未確認', self.gui.plan_note.set.call_args.args[0])
        self.assertNotIn('前回取得', self.gui.plan_note.set.call_args.args[0])
        self.assertNotIn('前回取得', self.gui.connection_note.set.call_args.args[0])

    def test_review_action_reveals_blocked_row_despite_advanced_filters(self):
        tid = self.fixture.add(1, '要確認の対象', state='blocked')
        self.gui.simple_mode = False
        for name, initial in (('view_filter', 'すべて'), ('search', '一致しない検索'), ('filter', '実行・監視中')):
            variable = Mock()
            variable.get.return_value = initial
            variable.set.side_effect = lambda value, obj=variable: setattr(obj.get, 'return_value', value)
            setattr(self.gui, name, variable)
        self.gui.show_review_targets()
        self.assertEqual(self.fixture.order(), [tid])

    def test_countdown_expires_monitor_label_without_connection_work(self):
        self.gui.closing = False
        self.gui.render_usage = Mock()
        self.gui.auto_status = Mock()
        self.gui.auto_status.get.return_value = '監視中：利用枠の回復を待っています'
        self.gui.last_monitor_at = time.monotonic() - 91
        self.gui.display_auto_policy = {'enabled': True}
        self.gui.display_auto_config = {}
        self.gui.display_auto_ready = True
        self.gui.refresh_auto_status = Mock()
        with patch('app.connection_options') as load, patch('app.connection_enabled') as attest:
            self.gui.display_tick()
        load.assert_not_called(); attest.assert_not_called()
        self.gui.refresh_auto_status.assert_not_called()
        self.assertIn('接続確認中', self.gui.auto_status.set.call_args.args[0])
