from contextlib import closing
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

import app
import core
import connection_settings
import queue_state


class QueueSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = core.connection(Path(self.temp.name) / 'state')
        self.addCleanup(self.db.close)
        self.config = {'mode': 'queue', 'executable': '/fixture/cli', 'sha256': 'a' * 64,
                       'codex_home': '/fixture/home', 'desktop_confirmed': False,
                       'manual_exclusion_acknowledged': False}

    def pending_dispatch(self):
        connection_settings._store(self.db, self.config)
        thread, failure = str(uuid.uuid4()), str(uuid.uuid4())
        self.db.execute("INSERT INTO threads (id,title,enabled,state,failure_turn,generation) VALUES (?,'fixture',1,'claimed',?,1)",
                        (thread, failure))
        intent = core.record_intent(self.db, thread)
        queue_state.initialise(self.db)
        return queue_state.start(self.db, thread, intent)

    def snapshot(self):
        return {name: self.db.execute('SELECT * FROM ' + name).fetchall()
                for name in ('connection_settings', 'desktop_sessions', 'threads', 'attempts', 'queue_dispatches')}

    def test_pending_dispatch_preserves_connection_and_all_state_on_replacement(self):
        for status in ('sending', 'queued', 'unknown'):
            with self.subTest(status=status):
                row = self.pending_dispatch()
                if status == 'queued':
                    queue_state.accepted(self.db, row, str(uuid.uuid4()))
                elif status == 'unknown':
                    queue_state.uncertain(self.db, row)
                before = self.snapshot()
                with self.assertRaises(ValueError):
                    connection_settings._store(self.db, {'mode': 'prepared'})
                self.assertFalse(self.db.in_transaction)
                self.assertEqual(self.snapshot(), before)
                self.db.execute('DELETE FROM queue_dispatches')
                self.db.execute('DELETE FROM attempts')
                self.db.execute('DELETE FROM threads')

    def test_off_does_not_discard_pending_cleanup_connection(self):
        import auto_watch
        self.pending_dispatch()
        auto_watch.initialize(self.db)
        auto_watch.set_enabled(self.db, False)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            connection_settings._store(self.db, {'mode': 'guarded'})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_terminal_dispatch_allows_connection_replacement(self):
        for terminal in ('reached', 'cancelled'):
            with self.subTest(terminal=terminal):
                row = self.pending_dispatch()
                if terminal == 'reached':
                    queue_state.reached(self.db, row, str(uuid.uuid4()))
                else:
                    queue_state.cancelled(self.db, row)
                connection_settings._store(self.db, {'mode': 'prepared'})
                self.assertEqual(connection_settings.load(self.db), {'mode': 'prepared'})

    def test_outer_transaction_pending_guard_keeps_prior_state(self):
        self.pending_dispatch()
        before = self.snapshot()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(ValueError):
                connection_settings._store_in_transaction(self.db, {'mode': 'prepared'})
            self.assertTrue(self.db.in_transaction)
            self.assertEqual(self.snapshot(), before)
        finally:
            self.db.execute('ROLLBACK')

    def test_queue_setup_requires_live_native_binding_and_explicit_acknowledgement(self):
        import queue_bridge
        bridge = Mock()
        bridge.capabilities.return_value = {'desktop_binding': True, 'guarded_admission': True}
        with patch.object(queue_bridge, 'QueueBridge') as factory:
            factory.return_value.__enter__.return_value = bridge
            connection_settings.configure_queue(self.db, self.config, manual_exclusion_acknowledged=True)
        saved = connection_settings.load(self.db)
        self.assertEqual(saved['mode'], 'queue')
        self.assertTrue(connection_settings.enabled(saved))
        self.assertTrue(saved['desktop_confirmed'])
        self.assertTrue(saved['manual_exclusion_acknowledged'])

    def test_unverified_or_unacknowledged_setup_never_writes_settings(self):
        import queue_bridge
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged), patch.object(queue_bridge, 'QueueBridge') as factory:
                factory.return_value.__enter__.return_value.capabilities.return_value = {
                    'desktop_binding': False, 'guarded_admission': False}
                with self.assertRaises(ValueError):
                    connection_settings.configure_queue(self.db, self.config,
                                                         manual_exclusion_acknowledged=acknowledged)
            self.assertEqual(connection_settings.load(self.db), {})

    def test_gui_connection_uses_the_same_non_restart_route_on_both_platforms(self):
        for platform in ('win32', 'darwin'):
            gui = app.GUI.__new__(app.GUI)
            gui.root, gui.db, gui.closing = Mock(), self.db, False
            gui.update_connection_note, gui.reload_rows, gui.render_rows = Mock(), Mock(), Mock()
            gui.refresh_auto_status, gui.request_usage = Mock(), Mock()
            def save_config(db, config, *, before_store=None):
                connection_settings._store(db, dict(config, desktop_confirmed=True,
                                                     manual_exclusion_acknowledged=True),
                                           before_store=before_store)
            with self.subTest(platform=platform), patch('app.sys.platform', platform), patch(
                    'app.prepare_queue_connection', return_value=self.config) as prepare, patch(
                    'app.save_queue_connection', side_effect=save_config) as save, patch('app.messagebox') as dialogs, patch(
                    'app.start_auto_connection') as restart:
                dialogs.askyesno.return_value = True
                self.assertTrue(gui.request_desktop_connection(enable_auto_resume=True))
                gui.queue_setup_thread.join(2)
                gui.drain_queue_setup()
                gui.queue_setup_thread.join(2)
                gui.drain_queue_setup()
            prepare.assert_called_once_with()
            self.assertIsNot(save.call_args.args[0], self.db)
            self.assertEqual(save.call_args.args[1], self.config)
            self.assertTrue(callable(save.call_args.kwargs['before_store']))
            restart.assert_not_called()
            self.assertNotIn('再起動', dialogs.askyesno.call_args.args[1])
            self.assertNotIn('開き直', dialogs.askyesno.call_args.args[1])

    def test_gui_cancel_never_saves_or_enables(self):
        gui = app.GUI.__new__(app.GUI)
        gui.root, gui.db, gui.closing = Mock(), self.db, False
        with patch('app.prepare_queue_connection', return_value=self.config), patch(
                'app.save_queue_connection') as save, patch('app.set_auto_watch') as enable, patch(
                'app.messagebox') as dialogs:
            dialogs.askyesno.return_value = False
            self.assertTrue(gui.request_desktop_connection(enable_auto_resume=True))
            gui.queue_setup_thread.join(2)
            gui.drain_queue_setup()
            self.assertFalse(gui.queue_setup_running)
        save.assert_not_called()
        enable.assert_not_called()

    def test_before_store_callback_failure_rolls_back_without_writing(self):
        before = connection_settings.load(self.db)
        for callback in (lambda: False, Mock(side_effect=ValueError('fixture'))):
            with self.assertRaises(ValueError):
                connection_settings._store(self.db, {'mode': 'queue'}, before_store=callback)
            self.assertFalse(self.db.in_transaction)
            self.assertEqual(connection_settings.load(self.db), before)

    def test_native_validation_does_not_hold_write_lock(self):
        import queue_bridge
        home = Path(self.db.execute('PRAGMA database_list').fetchone()[2]).parent
        def probe():
            self.assertFalse(self.db.in_transaction)
            with closing(core.connection(home)) as other:
                other.execute('BEGIN IMMEDIATE')
                other.execute('ROLLBACK')
            bridge = Mock()
            bridge.capabilities.return_value = {'desktop_binding': True, 'guarded_admission': True}
            return bridge
        callback = Mock(side_effect=lambda: self.db.in_transaction)
        with patch.object(queue_bridge, 'QueueBridge') as factory:
            factory.return_value.__enter__.side_effect = probe
            connection_settings.configure_queue(self.db, self.config,
                manual_exclusion_acknowledged=True, before_store=callback)
        callback.assert_called_once_with()
        self.assertTrue(connection_settings.enabled(connection_settings.load(self.db)))
