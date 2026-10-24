"""Regression tests for single-flight GUI scheduling without opening a display."""
import pathlib
import queue
import sqlite3
import unittest
from unittest.mock import Mock, patch

import app

APP = pathlib.Path(__file__).resolve().parents[1] / 'app.py'

class MonitorSchedulingTests(unittest.TestCase):
    def test_single_worker_guard_and_recovery(self):
        source = APP.read_text(encoding='utf-8')
        self.assertIn('self.monitor_running = False', source)
        self.assertIn('if not self.monitor_running:', source)
        self.assertIn("self.monitor_queue.put(result)", source)
        self.assertIn("self.root.after(30000, self.monitor_tick)", source)
        self.assertIn("from diagnostics import monitor_status", source)
        self.assertIn("self.write(monitor_status(result))", source)
        init = source.split('    def __init__(self, enable_tray=True):', 1)[1].split('    def monitor_tick(self):', 1)[0]
        self.assertIn('self.root.after(8000, self.monitor_tick)', init)
        self.assertIn('self.root.after(1000, self.drain_monitor)', init)

    def test_periodic_refresh_does_not_schedule_another_monitor(self):
        source = APP.read_text(encoding='utf-8')
        periodic = source.split('    def periodic(self):', 1)[1].split('    def start_tray(self):',1)[0]
        self.assertNotIn('self.monitor_tick)', periodic)
        self.assertNotIn('self.drain_monitor)', periodic)


class SchedulingRecoveryTests(unittest.TestCase):
    def gui(self):
        gui = app.GUI.__new__(app.GUI)
        gui.closing = False
        gui.db = Mock()
        gui.root = Mock()
        gui.write = Mock()
        gui.monitor_running = False
        gui.monitor_queue = queue.Queue()
        gui.usage_running = False
        gui.usage_queue = queue.Queue(maxsize=1)
        gui.usage_status = Mock()
        gui.last_usage_result = {}
        for method in ('refresh', 'refresh_auto_status', 'reload_rows', 'render_rows',
                       'render_usage', 'request_usage', 'update_connection_note'):
            setattr(gui, method, Mock())
        return gui

    def test_読取りや表示の失敗でも監視の予約を続けて次回起動できる(self):
        for stage in ('方針', '接続設定', '表示'):
            with self.subTest(stage=stage):
                gui = self.gui()
                with patch('app.auto_watch_status', return_value={'enabled': True}) as policy, \
                        patch('app.connection_options', return_value={}) as config, \
                        patch('app.threading.Thread') as worker:
                    failing = {'方針': policy, '接続設定': config,
                               '表示': gui.refresh_auto_status}[stage]
                    failing.side_effect = sqlite3.OperationalError('非公開のエラー詳細')
                    gui.monitor_tick()
                    self.assertFalse(gui.monitor_running)
                    worker.assert_not_called()
                    gui.write.assert_called_once_with('監視を開始できません。次回再確認します')
                    gui.root.after.assert_called_once_with(30000, gui.monitor_tick)
                    failing.side_effect = None
                    gui.root.after.call_args.args[1]()
                    worker.return_value.start.assert_called_once()
                    self.assertTrue(gui.monitor_running)
                    gui.monitor_tick()
                    worker.return_value.start.assert_called_once()

    def test_監視スレッドの生成や開始失敗でも実行中の印を戻す(self):
        for stage in ('生成', '開始'):
            with self.subTest(stage=stage):
                gui = self.gui()
                with patch('app.auto_watch_status', return_value={'enabled': True}), \
                        patch('app.connection_options', return_value={}), \
                        patch('app.threading.Thread') as worker:
                    failing = worker if stage == '生成' else worker.return_value.start
                    failing.side_effect = RuntimeError('開始失敗')
                    gui.monitor_tick()
                    self.assertFalse(gui.monitor_running)
                    gui.root.after.assert_called_once_with(30000, gui.monitor_tick)
                    failing.side_effect = None
                    gui.root.after.call_args.args[1]()
                    self.assertTrue(gui.monitor_running)

    def test_各定期更新の例外でも次回予約を維持する(self):
        cases = (
            ('periodic', 'refresh', 30000, '一覧を更新できません。次回再確認します'),
            ('display_tick', 'render_usage', 1000, '表示を更新できません。次回再確認します'),
            ('usage_tick', 'render_usage', 60000, '利用枠の確認を開始できません。次回再確認します'),
            ('usage_tick', 'refresh_auto_status', 60000, '利用枠の確認を開始できません。次回再確認します'),
            ('usage_tick', 'request_usage', 60000, '利用枠の確認を開始できません。次回再確認します'),
            ('poll_desktop_connection', 'connection_options', 1000, '接続状態を確認できません。次回再確認します'),
            ('poll_desktop_connection', 'refresh_auto_status', 1000, '接続状態を確認できません。次回再確認します'),
            ('poll_desktop_connection', 'reload_rows', 1000, '接続状態を確認できません。次回再確認します'),
            ('poll_desktop_connection', 'render_rows', 1000, '接続状態を確認できません。次回再確認します'),
            ('poll_desktop_connection', 'request_usage', 1000, '接続状態を確認できません。次回再確認します'),
        )
        for method, stage, delay, message in cases:
            with self.subTest(method=method, stage=stage):
                gui = self.gui()
                with patch('app.connection_options', return_value={'_connection_phase': 'ready'}) as config:
                    failing = config if stage == 'connection_options' else getattr(gui, stage)
                    failing.side_effect = OSError('非公開のエラー詳細')
                    callback = getattr(gui, method)
                    callback()
                    gui.root.after.assert_called_once_with(delay, callback)
                    gui.write.assert_called_once_with(message)
                    failing.side_effect = None
                    failing.reset_mock()
                    gui.root.after.call_args.args[1]()
                    failing.assert_called_once()

    def test_監視結果の反映失敗でも実行中の印を戻し次の受領を扱える(self):
        for stage in ('reload_rows', 'render_rows'):
            with self.subTest(stage=stage):
                gui = self.gui()
                gui.monitor_running = True
                gui.monitor_queue.put({'state': 'checked'})
                failing = getattr(gui, stage)
                failing.side_effect = sqlite3.OperationalError('読取り失敗')
                gui.drain_monitor()
                self.assertFalse(gui.monitor_running)
                gui.root.after.assert_called_once_with(1000, gui.drain_monitor)
                gui.write.assert_called_once_with('監視結果を反映できません。次回再確認します')
                failing.side_effect = None
                result = {'state': 'checked', '_ui_finished_at': 123}
                gui.monitor_queue.put(result)
                gui.root.after.call_args.args[1]()
                self.assertIs(gui.last_monitor_result, result)

    def test_利用枠結果の反映失敗でも予約を維持して次の受領を扱える(self):
        for stage in ('connection_options', 'update_connection_note', 'render_usage'):
            with self.subTest(stage=stage):
                gui = self.gui()
                gui.usage_running = True
                gui.usage_queue.put(('{}', {}))
                with patch('app.connection_options', return_value={}) as config:
                    failing = config if stage == 'connection_options' else getattr(gui, stage)
                    failing.side_effect = OSError('読取り失敗')
                    gui.drain_usage()
                    self.assertFalse(gui.usage_running)
                    gui.root.after.assert_called_once_with(500, gui.drain_usage)
                    gui.write.assert_called_once_with('利用枠の取得結果を反映できません。次回再確認します')
                    failing.side_effect = None
                    gui.update_connection_note.reset_mock()
                    gui.usage_queue.put(('{}', {}))
                    gui.root.after.call_args.args[1]()
                    gui.update_connection_note.assert_called_once_with({}, config={})

    def test_利用枠スレッドの生成失敗でも実行中の印を戻す(self):
        gui = self.gui()
        with patch('app.connection_options', return_value={}), \
                patch('app.threading.Thread', side_effect=RuntimeError('生成失敗')):
            app.GUI.request_usage(gui)
        self.assertFalse(gui.usage_running)
        self.assertTrue(gui.usage_failed)

    def test_失敗のログ記録でも例外が出た場合に次回予約を維持する(self):
        for method, delay in (('monitor_tick', 30000), ('display_tick', 1000),
                              ('usage_tick', 60000), ('drain_monitor', 1000),
                              ('drain_usage', 500), ('poll_desktop_connection', 1000),
                              ('periodic', 30000)):
            with self.subTest(method=method):
                gui = self.gui()
                gui.monitor_running = method == 'drain_monitor'
                gui.monitor_queue.put({'state': 'checked'})
                gui.usage_queue.put(('{}', {}))
                for operation in (gui.refresh, gui.render_usage, gui.reload_rows):
                    operation.side_effect = OSError('更新失敗')
                gui.write.side_effect = RuntimeError('ログ更新失敗')
                callback = getattr(gui, method)
                with patch('app.auto_watch_status', return_value={'enabled': True}), \
                        patch('app.connection_options', side_effect=OSError('読取り失敗')):
                    with self.assertRaisesRegex(RuntimeError, 'ログ更新失敗'):
                        callback()
                self.assertFalse(gui.monitor_running)
                gui.root.after.assert_called_once_with(delay, callback)

    def test_終了中は定期処理を再予約しない(self):
        for method in ('monitor_tick', 'display_tick', 'usage_tick', 'drain_monitor',
                       'drain_usage', 'poll_desktop_connection', 'periodic'):
            with self.subTest(method=method):
                gui = self.gui()
                gui.closing = True
                getattr(gui, method)()
                gui.root.after.assert_not_called()

if __name__ == '__main__':
    unittest.main()
