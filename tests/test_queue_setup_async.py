import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import auto_watch
import connection_settings
import core


class Root:
    def __init__(self):
        self.owner = threading.get_ident()
        self.callbacks = []

    def after(self, delay, callback):
        if threading.get_ident() != self.owner:
            raise AssertionError('worker touched Tk')
        self.callbacks.append(callback)


class QueueSetupAsyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = core.connection(Path(self.temp.name) / 'state')
        self.addCleanup(self.db.close)
        auto_watch.initialize(self.db)
        self.config = {'mode': 'queue', 'executable': '/fixture/cli', 'sha256': 'a' * 64,
                       'codex_home': '/fixture/home', 'desktop_confirmed': False,
                       'manual_exclusion_acknowledged': False}
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.root, self.gui.db, self.gui.closing = Root(), self.db, False
        for name in ('update_connection_note', 'reload_rows', 'render_rows', 'request_usage',
                     'refresh_auto_status'):
            setattr(self.gui, name, Mock())
        self.gui.auto_status = Mock()
        self.owner = threading.get_ident()

    def finish_stage(self):
        self.gui.queue_setup_thread.join(timeout=2)
        self.assertFalse(self.gui.queue_setup_thread.is_alive())
        self.gui.drain_queue_setup()

    def save(self, db, config, *, before_store=None):
        self.assertNotEqual(threading.get_ident(), self.owner)
        candidate = dict(config, desktop_confirmed=True, manual_exclusion_acknowledged=True)
        connection_settings._store(db, candidate, before_store=before_store)

    def test_prepare_is_background_and_mainthread_remains_responsive(self):
        entered, release = threading.Event(), threading.Event()
        def prepare():
            self.assertNotEqual(threading.get_ident(), self.owner)
            entered.set()
            release.wait(2)
            return self.config
        try:
            with patch('app.prepare_queue_connection', side_effect=prepare), patch('app.messagebox') as dialogs:
                started = time.monotonic()
                self.assertTrue(self.gui.request_desktop_connection(enable_auto_resume=True))
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.gui.request_desktop_connection(enable_auto_resume=True))
                self.assertTrue(self.gui.root.callbacks)
                dialogs.askyesno.assert_not_called()
                release.set()
                self.gui.queue_setup_thread.join(2)
        finally:
            release.set()

    def test_display_refresh_keeps_setup_progress_and_cancel_action(self):
        self.gui.queue_setup_running = True
        self.gui.auto_button = Mock()
        app.GUI.render_auto_status(self.gui, {'enabled': False}, {}, False)
        self.assertTrue(self.gui.auto_status.set.call_args.args[0].startswith('接続確認中'))
        self.assertIn('中止', self.gui.auto_button.configure.call_args.kwargs['text'])
        self.assertNotIn('をON', self.gui.auto_button.configure.call_args.kwargs['text'])

    def test_consent_only_on_ui_then_separate_database_save_then_explicit_enable(self):
        def consent(*args, **kwargs):
            self.assertEqual(threading.get_ident(), self.owner)
            return True
        with patch('app.prepare_queue_connection', return_value=self.config), \
                patch('app.save_queue_connection', side_effect=self.save), patch('app.messagebox') as dialogs:
            dialogs.askyesno.side_effect = consent
            self.gui.request_desktop_connection(enable_auto_resume=True)
            self.finish_stage()
            self.finish_stage()
        self.assertTrue(auto_watch.status(self.db)['enabled'])
        self.assertTrue(connection_settings.enabled(connection_settings.load(self.db)))

    def test_reconnect_never_enables_previously_off_policy(self):
        with patch('app.prepare_queue_connection', return_value=self.config), \
                patch('app.save_queue_connection', side_effect=self.save), patch('app.messagebox') as dialogs:
            dialogs.askyesno.return_value = True
            self.gui.reconnect_auto_watch()
            self.finish_stage()
            self.finish_stage()
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_cancel_dialog_never_starts_save_or_enables(self):
        with patch('app.prepare_queue_connection', return_value=self.config), \
                patch('app.save_queue_connection') as save, patch('app.messagebox') as dialogs:
            dialogs.askyesno.return_value = False
            self.gui.request_desktop_connection(enable_auto_resume=True)
            self.finish_stage()
        save.assert_not_called()
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_off_during_preparation_invalidates_late_result(self):
        with patch('app.prepare_queue_connection', return_value=self.config), \
                patch('app.save_queue_connection') as save, patch('app.messagebox') as dialogs:
            self.gui.request_desktop_connection(enable_auto_resume=True)
            self.gui.toggle_auto_watch()
            self.finish_stage()
        save.assert_not_called()
        dialogs.askyesno.assert_not_called()
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_off_during_save_rejects_store_and_late_enable(self):
        entered, release = threading.Event(), threading.Event()
        def slow_save(db, config, *, before_store=None):
            entered.set()
            release.wait(2)
            self.save(db, config, before_store=before_store)
        try:
            with patch('app.prepare_queue_connection', return_value=self.config), \
                    patch('app.save_queue_connection', side_effect=slow_save), patch('app.messagebox') as dialogs:
                dialogs.askyesno.return_value = True
                self.gui.request_desktop_connection(enable_auto_resume=True)
                self.finish_stage()
                self.assertTrue(entered.wait(1))
                self.gui.toggle_auto_watch()
                release.set()
                self.finish_stage()
            self.assertEqual(connection_settings.load(self.db), {})
            self.assertFalse(auto_watch.status(self.db)['enabled'])
        finally:
            release.set()

    def test_settings_change_during_background_validation_survives_late_save(self):
        entered, release = threading.Event(), threading.Event()
        def slow_save(db, config, *, before_store=None):
            entered.set()
            release.wait(2)
            self.save(db, config, before_store=before_store)
        try:
            with patch('app.prepare_queue_connection', return_value=self.config), \
                    patch('app.save_queue_connection', side_effect=slow_save), patch('app.messagebox') as dialogs:
                dialogs.askyesno.return_value = True
                self.gui.request_desktop_connection(enable_auto_resume=True)
                self.finish_stage()
                self.assertTrue(entered.wait(1))
                connection_settings._store(self.db, {'mode': 'prepared', 'sha256': 'changed'})
                release.set()
                self.finish_stage()
            self.assertEqual(connection_settings.load(self.db), {'mode': 'prepared', 'sha256': 'changed'})
            self.assertFalse(auto_watch.status(self.db)['enabled'])
        finally:
            release.set()

    def test_off_between_ui_completion_check_and_enable_transaction_wins(self):
        real_enable = auto_watch.set_enabled
        def race(db, enabled, *, before_change=None):
            real_enable(db, False)
            return real_enable(db, enabled, before_change=before_change)
        with patch('app.prepare_queue_connection', return_value=self.config), \
                patch('app.save_queue_connection', side_effect=self.save), patch('app.messagebox') as dialogs:
            dialogs.askyesno.return_value = True
            self.gui.request_desktop_connection(enable_auto_resume=True)
            self.finish_stage()
            self.gui.queue_setup_thread.join(2)
            with patch('app.set_auto_watch', side_effect=race):
                self.gui.drain_queue_setup()
        self.assertFalse(auto_watch.status(self.db)['enabled'])
        self.assertFalse(self.db.in_transaction)

    def test_enable_transaction_callback_failure_rolls_back(self):
        before = auto_watch.status(self.db)
        for callback in (lambda: False, Mock(side_effect=ValueError('fixture'))):
            with self.assertRaises(ValueError):
                auto_watch.set_enabled(self.db, True, before_change=callback)
            self.assertEqual(auto_watch.status(self.db), before)
            self.assertFalse(self.db.in_transaction)

    def test_closing_or_settings_change_invalidates_late_preparation(self):
        for change in ('closing', 'settings'):
            self.gui.closing = False
            with patch('app.prepare_queue_connection', return_value=self.config), \
                    patch('app.save_queue_connection') as save, patch('app.messagebox') as dialogs:
                self.gui.request_desktop_connection(enable_auto_resume=True)
                if change == 'closing':
                    self.gui.closing = True
                else:
                    connection_settings._store(self.db, {'mode': 'prepared'})
                self.finish_stage()
                save.assert_not_called()
                dialogs.askyesno.assert_not_called()
            self.gui.queue_setup_running = False
        self.assertFalse(auto_watch.status(self.db)['enabled'])


if __name__ == '__main__':
    unittest.main()
