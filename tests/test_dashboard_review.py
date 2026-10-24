"""後着レビューの表示退行を通常mock GUIで検査する。実接続は行わない。"""
import json
import queue
import time
import unittest
from unittest.mock import Mock, patch

import app
from tests.test_usage_ui import sample


class DashboardReview(unittest.TestCase):
    def setUp(self):
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.db = Mock()
        self.gui.root = Mock()
        self.gui.auto_status = Mock()
        self.gui.auto_button = Mock()
        self.gui.connection_action = Mock()
        self.gui.connection_note = Mock()
        self.gui.plan_note = Mock()
        self.gui.usage_status = Mock()
        self.gui.quota_cards = Mock()
        self.gui.closing = False
        self.gui.rows = []
        self.gui.known = []
        self.policy = {'enabled': True, 'generation': 2}
        self.config = {'mode': 'managed'}
        for name, value in [('auto_watch_status', self.policy),
                            ('connection_options', self.config),
                            ('connection_enabled', True)]:
            patcher = patch('app.' + name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def receipt(self, state='checked', results=None):
        self.gui.last_monitor_result = {'state': state, 'results': results or []}
        self.gui.last_monitor_policy = dict(self.policy)
        self.gui.last_monitor_config = dict(self.config)
        self.gui.last_monitor_at = time.monotonic()

    def status(self):
        self.gui.refresh_auto_status()
        return self.gui.auto_status.set.call_args.args[0]

    def test_current_blocked_target_overrides_empty_successful_monitor_results(self):
        for state in ('checked', 'quota-wait'):
            with self.subTest(state=state):
                self.receipt(state)
                self.gui.rows = [{'id': 'fixture', 'state': 'blocked'}]
                self.gui.known = [{'id': 'fixture'}]
                text = self.status()
                self.assertIn('要確認', text)
                self.assertIn('対象', text)
                self.assertNotIn('監視中', text)
                self.assertNotIn('修復', text)

    def test_missing_blocked_row_does_not_require_review(self):
        self.receipt()
        self.gui.rows = [{'id': 'fixture', 'state': 'blocked'}]
        self.assertNotIn('要確認', self.status())

    def test_excluded_blocked_row_does_not_require_review(self):
        for reason in ('archived', 'subagent', 'noninteractive'):
            with self.subTest(reason=reason):
                self.receipt()
                self.gui.rows = [{'id': 'fixture', 'state': 'blocked'}]
                self.gui.known = [{'id': 'fixture', 'excluded_reason': reason}]
                self.assertNotIn('要確認', self.status())

    def test_review_action_displays_visible_blocked_row(self):
        from tests.test_resume_list import ResumeList
        fixture = ResumeList()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.gui = fixture.gui
        self.gui.auto_status = Mock()
        self.gui.connection_action = Mock()
        self.gui.simple_mode = True
        self.gui.view_filter = Mock()
        self.gui.view_filter.get.return_value = '再開待ち'
        self.gui.view_filter.set.side_effect = lambda value: setattr(
            self.gui.view_filter.get, 'return_value', value)
        visible = fixture.add(1, '確認するチャット', state='blocked')
        fixture.add(2, '検出から消えたチャット', state='blocked')
        fixture.add(3, '除外したチャット', state='blocked')
        self.gui.known = [self.gui.known[0],
                          dict(self.gui.known[2], excluded_reason='archived')]
        self.receipt()
        self.assertIn('要確認', self.status())
        action = self.gui.connection_action.configure.call_args.kwargs
        self.assertEqual(action['text'], '要確認のチャットを表示')
        action['command']()
        self.assertEqual(fixture.order(), [visible])
        self.gui.search.set.assert_called_once_with('')
        self.gui.filter.set.assert_called_once_with('すべて')

    def test_previous_blocked_result_does_not_leak_across_policy_or_config_change(self):
        for change in ('policy', 'config'):
            with self.subTest(change=change):
                self.receipt(results=[('fixture', 'blocked')])
                if change == 'policy':
                    self.gui.last_monitor_policy = {'enabled': True, 'generation': 1}
                else:
                    self.gui.last_monitor_config = {'mode': 'guarded'}
                text = self.status()
                self.assertIn('接続確認中', text)
                self.assertNotIn('要確認', text)
                self.assertNotIn('監視中', text)

    @patch('quota_presentation.time.time', return_value=1800000000)
    def test_failed_quota_read_marks_connection_and_plan_historical_without_losing_values(self, current_time):
        reading = sample()
        reading['checked_at'] = current_time.return_value
        reading.update(plan_type='plus', desktop_connection='read', peer_status='read',
                       account_match=True, peer_limits=sample() | {
                           'plan_type': 'plus', 'checked_at': current_time.return_value})
        self.gui.last_usage_result = reading
        self.gui.update_connection_note(reading)
        self.assertIn('読取り成功', self.gui.connection_note.set.call_args.args[0])
        self.assertIn('Plus', self.gui.plan_note.set.call_args.args[0])
        self.gui.usage_queue = queue.Queue(maxsize=1)
        self.gui.usage_queue.put((json.dumps(self.config, sort_keys=True), None))
        self.gui.usage_running = True
        self.gui.drain_usage()
        self.assertIs(self.gui.last_usage_result, reading)
        self.assertTrue(self.gui.usage_failed)
        view = self.gui.quota_cards.render.call_args.args[0]
        self.assertTrue(view['stale'])
        connection_note = self.gui.connection_note.set.call_args.args[0]
        self.assertIn('前回取得', connection_note)
        self.assertIn('最新の読取りは失敗', connection_note)
        plan_note = self.gui.plan_note.set.call_args.args[0]
        self.assertIn('前回取得', plan_note)
        self.assertIn('Plus', plan_note)
        self.assertNotEqual(self.gui.desktop_read_state, 'read')

    def test_display_tick_only_repaints_cached_usage(self):
        self.gui.last_usage_result = sample()
        self.gui.refresh_auto_status = Mock()
        with patch('app.read_usage_status') as reader, patch('app.monitor_once') as monitor, \
                patch('app.DesktopBridge') as bridge:
            self.gui.display_tick()
        self.connection_options.assert_not_called()
        self.connection_enabled.assert_not_called()
        self.gui.refresh_auto_status.assert_not_called()
        reader.assert_not_called()
        monitor.assert_not_called()
        bridge.assert_not_called()
        self.gui.quota_cards.render.assert_called_once()
        self.gui.root.after.assert_called_once_with(1000, self.gui.display_tick)


if __name__ == '__main__':
    unittest.main()
