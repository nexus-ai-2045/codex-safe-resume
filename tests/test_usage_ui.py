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

    def test_valid_pin_remains_preferred_without_official_bundle_lookup(self):
        config = {'mode': 'prepared', 'sha256': 'saved'}
        pinned = Path('/saved/official/codex')
        with patch('app.validate_executable', return_value=pinned) as validate, \
                patch('queue_platform.verify_darwin_cli_files', create=True) as verify:
            self.assertEqual(app.usage_executable(config), pinned)
        validate.assert_called_once_with(config, require_confirmation=False)
        verify.assert_not_called()

    def test_stale_pin_and_unconfigured_mac_use_same_verified_fixed_bundle(self):
        bundle = Path('/Applications/ChatGPT.app')
        cli = bundle / 'Contents/Resources/codex-cli/bin/codex'
        for config in ({'mode': 'prepared', 'executable': '/arbitrary/codex', 'sha256': 'old'}, {}):
            with self.subTest(configured=bool(config)):
                original = dict(config)
                with patch('app.sys.platform', 'darwin'), \
                        patch('app.validate_executable', side_effect=ValueError('stale pin')), \
                        patch('app.validate_location', side_effect=AssertionError('unverified source')), \
                        patch('queue_platform.verify_darwin_cli_files', create=True) as verify, \
                        patch('app.monitor_once') as sender:
                    self.assertEqual(app.usage_executable(config), cli)
                verify.assert_called_once_with(bundle, [cli], '2DC432GLL2')
                sender.assert_not_called()
                self.assertEqual(config, original)

    def test_invalid_official_bundle_does_not_rehash_or_execute_saved_path(self):
        with patch('app.sys.platform', 'darwin'), \
                patch('app.validate_executable', side_effect=ValueError('stale')) as validate, \
                patch('queue_platform.verify_darwin_cli_files', side_effect=ValueError('signature'), create=True), \
                patch('app.read_limits') as read:
            with self.assertRaises(ValueError):
                app.read_usage_status({'mode': 'prepared', 'executable': '/arbitrary/codex'})
        self.assertEqual(validate.call_count, 2)
        read.assert_not_called()

    def test_changed_pin_never_uses_a_windows_fallback(self):
        with patch('app.sys.platform', 'win32'), \
                patch('app.validate_executable', side_effect=ValueError), \
                patch('queue_platform.verify_darwin_cli_files', create=True) as verify:
            with self.assertRaises(ValueError):
                app.usage_executable({'mode': 'prepared'})
        verify.assert_not_called()

    def test_unconfigured_windows_has_no_resolver_or_staging(self):
        with patch('app.sys.platform', 'win32'), \
                patch('queue_platform.verify_darwin_cli_files', create=True) as verify, \
                patch('windows_runtime.stage_cli') as stage:
            with self.assertRaises(ValueError):
                app.usage_executable({})
        verify.assert_not_called()
        stage.assert_not_called()

    def test_display_fallback_preserves_original_peer_config_and_send_stop(self):
        config = {'mode': 'prepared', 'executable': '/saved/codex', 'sha256': 'old'}
        original = dict(config)
        result = dict(sample(), peer_status='not_provided', account_match=None)
        before = self.db.total_changes
        with patch('app.sys.platform', 'darwin'), \
                patch('app.validate_executable', side_effect=ValueError('stale')), \
                patch('queue_platform.verify_darwin_cli_files', create=True) as verify, \
                patch('app.DesktopBridge', side_effect=ValueError('old pin')) as peer, \
                patch('app.read_limits', return_value=result) as read, \
                patch('app.monitor_once') as sender:
            actual = app.read_usage_status(config)
        verify.assert_called_once()
        peer.assert_not_called()
        read.assert_called_once_with(Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'), peer_reply=None)
        self.assertEqual(actual['desktop_connection'], 'unavailable')
        self.assertIsNone(actual['account_match'])
        self.assertIn('自動送信: 停止', app.connection_text(config, actual))
        self.assertEqual(config, original)
        self.assertEqual(self.db.total_changes, before)
        sender.assert_not_called()

    def test_valid_pin_is_checked_before_peer_then_resolved_once_before_cli(self):
        config = {'mode': 'prepared'}
        order = []
        probe = Mock()
        probe.__enter__ = Mock(return_value=probe)
        probe.__exit__ = Mock(return_value=False)
        probe.quota.side_effect = lambda: order.append('peer') or {'rateLimits': {}}
        def resolve(_):
            order.append('final-resolve')
            return Path('/fixture/codex')
        def validate(*args, **kwargs):
            order.append('saved-pin')
            return Path('/fixture/codex')
        def read(*args, **kwargs):
            order.append('read-cli')
            return dict(sample(), peer_status='read', account_match=None)
        with patch('app.validate_executable', side_effect=validate) as saved, \
                patch('app.DesktopBridge', return_value=probe) as peer, \
                patch('app.usage_executable', side_effect=resolve) as resolver, \
                patch('app.read_limits', side_effect=read):
            app.read_usage_status(config)
        self.assertEqual(order, ['saved-pin', 'peer', 'final-resolve', 'read-cli'])
        saved.assert_called_once_with(config, require_confirmation=False)
        resolver.assert_called_once_with(config)
        peer.assert_called_once_with(config=config, transport_only=True, timeout=3)

    def test_unconfigured_display_verifies_bundle_once_and_never_contacts_peer(self):
        with patch('app.sys.platform', 'darwin'), \
                patch('queue_platform.verify_darwin_cli_files') as verify, \
                patch('app.DesktopBridge') as peer, \
                patch('app.read_limits', return_value=dict(sample(), account_match=None)):
            result = app.read_usage_status({})
        verify.assert_called_once()
        peer.assert_not_called()
        self.assertEqual(result['desktop_connection'], 'not_configured')


if __name__ == '__main__': unittest.main()
