"""利用枠の表示は送信・DB変更・Tk worker操作から独立させる。"""
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import app
import core


def sample():
    return dict(checked_at=1800000000, account_bound=True, ordinary_usage_allowed=True,
                windows=[dict(slot='primary', duration_minutes=10080, used_percent=25,
                              remaining_percent=75, resets_at=1900000000)])


class UsageUi(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = core.connection(Path(self.temp.name)/'state')
        self.addCleanup(self.db.close)
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.db = self.db
        self.gui.root = Mock()
        self.gui.closing = False
        self.gui.tray = None
        self.gui.usage_running = False
        self.gui.usage_thread = None
        self.gui.usage_queue = queue.Queue(maxsize=1)
        self.gui.usage_status = Mock()
        self.gui.connection_note = Mock()

    def test_weekly_primary_is_never_displayed_as_five_hours(self):
        text = app.usage_text(sample())
        self.assertIn('1週間枠: 制限あり・残り75%', text)
        self.assertIn('5時間枠: 未確認（APIから未提供）', text)
        self.assertIn('取得', text)
        self.assertIn('公式CLI', text)

    def test_display_countdown_updates_never_fetch_or_send(self):
        self.gui.last_usage_result = sample()
        self.gui.quota_cards = Mock()
        self.gui.usage_updated = Mock()
        self.gui.refresh_auto_status = Mock()
        self.gui.request_usage = Mock()
        before = self.db.total_changes
        with patch('app.monitor_once') as sender:
            self.gui.display_tick()
        self.gui.request_usage.assert_not_called()
        sender.assert_not_called()
        self.assertEqual(self.db.total_changes, before)
        self.gui.quota_cards.render.assert_called_once()

    def test_failed_fetch_preserves_last_value_as_historical_not_current(self):
        self.gui.last_usage_result = sample()
        self.gui.quota_cards = Mock()
        self.gui.usage_updated = Mock()
        self.gui.usage_running = True
        self.gui.usage_queue.put(('{}', None))
        before = self.db.total_changes
        with patch('app.connection_options', return_value={}):
            self.gui.drain_usage()
        self.assertEqual(self.gui.last_usage_result, sample())
        view = self.gui.quota_cards.render.call_args.args[0]
        self.assertTrue(view['stale'])
        self.assertIn('更新待ち', view['updated_text'])
        self.assertEqual(self.db.total_changes, before)

    def test_worker_updates_only_queue_without_enabling_or_sending(self):
        changes = self.db.total_changes
        with patch('app.usage_executable', return_value='fixture'), patch('app.read_limits', return_value=sample()), patch('app.monitor_once') as sender:
            self.gui.request_usage()
            self.gui.usage_thread.join(1)
            self.assertFalse(self.gui.usage_thread.is_alive())
            self.gui.usage_status.set.assert_not_called()
            self.gui.drain_usage()
            sender.assert_not_called()
        self.assertEqual(self.db.total_changes, changes)
        self.assertIn('1週間枠: 制限あり・残り75%', self.gui.usage_status.set.call_args.args[0])
        self.assertFalse(self.gui.usage_running)

    def test_only_one_usage_request_runs_at_a_time(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def read(_):
            release.wait(1)
            return sample()
        with patch('app.usage_executable', return_value='fixture'), patch('app.read_limits', side_effect=read) as reader:
            self.gui.request_usage()
            self.gui.request_usage()
            release.set()
            self.gui.usage_thread.join(2)
            self.assertFalse(self.gui.usage_thread.is_alive())
            self.assertEqual(reader.call_count, 1)

    def test_changed_connection_discards_old_result(self):
        self.gui.usage_running = True
        self.gui.usage_queue.put(('{}', sample()))
        self.gui.request_usage = Mock()
        with patch('app.connection_options', return_value={'mode': 'prepared'}):
            self.gui.drain_usage()
        self.assertIn('取得元が変わり', self.gui.usage_status.set.call_args.args[0])
        self.assertNotIn('75%', self.gui.usage_status.set.call_args.args[0])
        self.gui.request_usage.assert_called_once()

    def test_raw_errors_are_not_displayed(self):
        with patch('app.usage_executable', side_effect=ValueError('private-error-value')):
            self.gui.request_usage()
            self.gui.usage_thread.join(1)
            self.gui.drain_usage()
        self.assertIn('取得できません', self.gui.usage_status.set.call_args.args[0])
        self.assertNotIn('private-error-value', self.gui.usage_status.set.call_args.args[0])
        self.assertIn('5時間枠: 未確認', self.gui.usage_status.set.call_args.args[0])
        self.assertIn('1週間枠: 未確認', self.gui.usage_status.set.call_args.args[0])

    def test_quit_waits_for_owned_read_cleanup_before_destroy(self):
        self.gui.db = Mock()
        self.gui.usage_thread = Mock()
        self.gui.usage_thread.is_alive.side_effect = [True, False]
        with patch('app.set_auto_watch'), patch('queue_state.pending', return_value=[]):
            self.gui.quit_app()
            self.assertTrue(self.gui.closing)
            self.gui.db.close.assert_not_called()
            self.gui.root.destroy.assert_not_called()
            self.gui.root.after.call_args.args[1]()
        self.gui.db.close.assert_called_once()
        self.gui.root.destroy.assert_called_once()

    def test_changed_pinned_cli_never_falls_back_to_another_source(self):
        with patch('app.validate_executable', side_effect=ValueError), patch('app.validate_location') as location:
            with self.assertRaises(ValueError):
                app.usage_executable({'mode': 'prepared'})
        location.assert_not_called()


if __name__ == '__main__': unittest.main()
