"""常駐アプリの通常操作は全体ON/OFFと残量確認だけにする。"""
from datetime import date
import unittest
from unittest.mock import Mock, patch

import app
from tests import test_gui_controls as controls
from tests.test_usage_ui import sample


class SimpleUI(unittest.TestCase):
    def gui(self):
        gui = controls.GuiControls().gui()
        gui.simple_mode = True
        gui.auto_status = Mock()
        gui.plan_note = Mock()
        gui.refresh_auto_status = Mock()
        gui.reload_rows = Mock()
        gui.render_rows = Mock()
        return gui

    def test_primary_action_changes_global_mode_without_selecting_chats(self):
        gui = self.gui()
        gui.toggle_auto_watch = Mock()
        records = controls.GuiControls().build(gui)
        buttons = {options.get('text'): options for kind, _, _, options in records if kind == 'Button'}
        buttons['自動再開をON']['command']()
        gui.toggle_auto_watch.assert_called_once()
        self.assertEqual(len(gui.advanced_widgets), 5)
        for widget in gui.advanced_widgets:
            widget.grid_remove.assert_called_once()
        self.assertIn(('title', 'auto', 'status', 'reset'),
                      [call.kwargs.get('displaycolumns') for call in gui.tree.configure.call_args_list])

    def test_off_stops_global_policy_without_per_chat_dialogs(self):
        gui = self.gui()
        gui.db = Mock()
        gui.request_usage = Mock()
        with patch('app.auto_watch_status', return_value={'enabled': True, 'generation': 1}), \
                patch('app.set_auto_watch') as setter, patch('app.start_auto_connection') as connect, \
                patch('app.messagebox') as dialogs:
            gui.toggle_auto_watch()
        setter.assert_called_once_with(gui.db, False)
        connect.assert_not_called()
        dialogs.askyesno.assert_not_called()

    def test_first_on_cannot_start_without_existing_connection(self):
        gui = self.gui()
        gui.db = Mock()
        gui.request_usage = Mock()
        with patch('app.sys.platform', 'darwin'), patch('app.auto_watch_status', return_value={'enabled': False, 'generation': 0}), \
                patch('app.connection_options', return_value={}), \
                patch('app.set_auto_watch') as setter, \
                patch('app.start_auto_connection') as connect, patch('app.messagebox') as dialogs, \
                patch('app.prepare_queue_connection', side_effect=ValueError('unavailable')):
            dialogs.askyesno.return_value = True
            gui.toggle_auto_watch()
        connect.assert_not_called()
        setter.assert_not_called()
        dialogs.askyesno.assert_not_called()
        self.assertIn('接続を確認できません', dialogs.showinfo.call_args.args[1])

    def test_connection_repair_cancel_never_changes_monitoring_or_launches(self):
        gui = self.gui()
        gui.db = Mock()
        with patch('app.sys.platform', 'darwin'), patch('app.auto_watch_status', return_value={'enabled': False}), \
                patch('app.start_auto_connection') as connect, patch('app.set_auto_watch') as setter, \
                patch('app.messagebox') as dialogs, \
                patch('app.prepare_queue_connection', side_effect=ValueError('unavailable')):
            dialogs.askyesno.return_value = False
            gui.reconnect_auto_watch()
        connect.assert_not_called()
        setter.assert_not_called()
        dialogs.askyesno.assert_not_called()
        self.assertIn('接続を確認できません', dialogs.showinfo.call_args.args[1])

    def test_cancel_first_connection_does_not_enable_or_launch(self):
        gui = self.gui()
        gui.db = Mock()
        with patch('app.auto_watch_status', return_value={'enabled': False}), \
                patch('app.connection_options', return_value={}), patch('app.set_auto_watch') as setter, \
                patch('app.start_auto_connection') as connect, patch('app.messagebox') as dialogs, \
                patch('app.prepare_queue_connection', side_effect=ValueError('unavailable')):
            dialogs.askyesno.return_value = False
            gui.toggle_auto_watch()
        setter.assert_not_called()
        connect.assert_not_called()

    def test_live_managed_connection_is_reused_before_quota_display_arrives(self):
        gui = self.gui()
        gui.db = Mock()
        gui.request_usage = Mock()
        with patch('app.auto_watch_status', return_value={'enabled': False}), \
                patch('app.connection_options', return_value={'mode': 'managed'}), \
                patch('app.connection_enabled', return_value=True), \
                patch('app.set_auto_watch') as setter, patch('app.start_auto_connection') as connect, \
                patch('app.messagebox') as dialogs:
            gui.toggle_auto_watch()
        setter.assert_called_once_with(gui.db, True)
        connect.assert_not_called()
        dialogs.askyesno.assert_not_called()

    def test_live_connection_with_pending_display_does_not_claim_sending_is_stopped(self):
        gui = self.gui()
        gui.db = Mock()
        with patch('app.auto_watch_status', return_value={'enabled': True}), \
                patch('app.connection_options', return_value={'mode': 'managed'}), \
                patch('app.connection_enabled', return_value=True):
            app.GUI.refresh_auto_status(gui)
        text = gui.auto_status.set.call_args.args[0]
        self.assertIn('接続確認中', text)
        self.assertNotIn('監視中', text)
        self.assertNotIn('まだ送信しません', text)

    def test_reset_countdown_is_relative_and_expired_time_never_claims_recovery(self):
        value = sample()
        value['windows'][0]['resets_at'] = 1900000000
        text = app.usage_text(value, today=date(2026, 10, 21), now=1900000000 - 7201)
        self.assertIn('あと2時間1分', text)
        expired = app.usage_text(value, now=1900000001)
        self.assertIn('回復時刻を経過・再確認待ち', expired)
        self.assertNotIn('回復済み', expired)

    def test_normal_view_ignores_hidden_advanced_filters(self):
        from tests import test_resume_list as fixtures
        fixture = fixtures.ResumeList()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        tid = fixture.add(1, '停止したチャット', limited=True)
        fixture.gui.simple_mode = True
        fixture.gui.search.get.return_value = '一致しない検索'
        fixture.gui.filter.get.return_value = '完了'
        fixture.gui.render_rows()
        self.assertEqual(fixture.order(), [tid])

    def test_failed_read_clears_the_previous_plan(self):
        gui = self.gui()
        gui.db = Mock()
        gui.connection_note = Mock()
        with patch('app.connection_options', return_value={}):
            app.GUI.update_connection_note(gui, dict(plan_type='plus'))
            self.assertIn('Plus', gui.plan_note.set.call_args.args[0])
            app.GUI.update_connection_note(gui)
        self.assertNotIn('Plus', gui.plan_note.set.call_args.args[0])
        self.assertIn('未確認', gui.plan_note.set.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
