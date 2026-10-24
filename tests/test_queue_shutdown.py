from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

import app
import auto_watch
import core
import queue_state


class QueueShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.temp).resolve() / 'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        auto_watch.initialize(self.db)
        auto_watch.set_enabled(self.db, True)
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.root, self.gui.write, self.gui.show_window = Mock(), Mock(), Mock()
        self.gui.auto_status = Mock()
        self.gui.db = core.connection(self.home)
        self.addCleanup(self.gui.db.close)
        self.gui.closing = False
        self.gui.tray = None
        self.gui.monitor_thread = None
        self.gui.usage_thread = None

    def dispatch(self):
        thread, failure = str(uuid.uuid4()), str(uuid.uuid4())
        self.db.execute("INSERT INTO threads (id,title,enabled,state,failure_turn,generation) VALUES (?,'fixture',1,'claimed',?,1)",
                        (thread, failure))
        intent = core.record_intent(self.db, thread)
        queue_state.initialise(self.db)
        return queue_state.start(self.db, thread, intent)

    def finish_worker(self):
        self.assertFalse(self.gui.shutdown_thread.daemon)
        self.gui.shutdown_thread.join(2)
        self.assertFalse(self.gui.shutdown_thread.is_alive())
        self.gui.finish_quit()

    def test_quit_revokes_final_send_permission_before_destroy(self):
        row = self.dispatch()
        self.gui.monitor_thread = Mock()
        self.gui.monitor_thread.is_alive.return_value = True
        self.gui.quit_app()
        self.assertFalse(auto_watch.status(self.db)['enabled'])
        self.assertFalse(queue_state.permitted(self.db, row))
        self.gui.root.destroy.assert_not_called()

    def test_quit_without_pending_disables_and_destroys(self):
        self.gui.quit_app()
        self.assertFalse(auto_watch.status(self.db)['enabled'])
        self.gui.root.destroy.assert_called_once()

    def test_durable_off_failure_keeps_window_and_database_open(self):
        with patch('app.set_auto_watch', side_effect=RuntimeError('fixture')):
            self.gui.quit_app()
        self.assertFalse(self.gui.closing)
        self.gui.root.destroy.assert_not_called()
        self.gui.db.execute('SELECT 1')
        self.gui.show_window.assert_called_once()

    def test_pending_cleanup_waits_for_existing_monitor_and_usage(self):
        self.dispatch()
        monitor, usage = Mock(), Mock()
        monitor.is_alive.return_value = True
        usage.is_alive.return_value = True
        self.gui.monitor_thread, self.gui.usage_thread = monitor, usage
        with patch('app.reconcile_pending_shutdown', return_value=True) as cleanup:
            self.gui.quit_app()
            cleanup.assert_not_called()
            monitor.is_alive.return_value = False
            self.gui.finish_quit()
            cleanup.assert_not_called()
            usage.is_alive.return_value = False
            self.gui.finish_quit()
            self.finish_worker()
        cleanup.assert_called_once_with(self.home)
        self.gui.root.destroy.assert_called_once()

    def test_unknown_cleanup_holds_quit_with_off_persisted(self):
        self.dispatch()
        with patch('app.reconcile_pending_shutdown', return_value=False):
            self.gui.quit_app()
            self.finish_worker()
        self.assertFalse(self.gui.closing)
        self.assertFalse(auto_watch.status(self.db)['enabled'])
        self.gui.root.destroy.assert_not_called()
        self.gui.db.execute('SELECT 1')
        self.gui.show_window.assert_called_once()

    def test_cleanup_exception_does_not_destroy_or_stop_native_codex(self):
        self.dispatch()
        with patch('app.reconcile_pending_shutdown', side_effect=OSError('private')):
            self.gui.quit_app()
            self.finish_worker()
        self.gui.root.destroy.assert_not_called()
        self.assertFalse(self.gui.closing)
        self.assertNotIn('private', str(self.gui.write.call_args))

    def test_tray_stop_failure_keeps_existing_backend_and_off(self):
        self.gui.tray = Mock()
        self.gui.tray.stop.side_effect = OSError('fixture')
        self.gui.quit_app()
        self.gui.root.destroy.assert_not_called()
        self.gui.show_window.assert_called_once()
        self.assertFalse(self.gui.closing)
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_cleanup_uses_separate_database_and_lease(self):
        self.dispatch()
        with patch('queue_bridge.QueueBridge') as factory, patch('queue_reconcile.reconcile') as reconcile:
            self.assertFalse(app.reconcile_pending_shutdown(self.home))
        other = reconcile.call_args.args[0]
        self.assertIsNot(other, self.db)
        self.assertEqual(factory.call_args.kwargs['timeout'], 3)
        self.assertTrue(factory.call_args.kwargs['for_pending'])
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_cleanup_batch_limit_does_not_start_transport(self):
        for _ in range(9):
            self.dispatch()
        with patch('queue_bridge.QueueBridge') as factory:
            self.assertFalse(app.reconcile_pending_shutdown(self.home))
        factory.assert_not_called()
        self.assertFalse(auto_watch.status(self.db)['enabled'])

    def test_cleanup_lease_conflict_keeps_pending_without_transport(self):
        self.dispatch()
        with core.monitor_lease(self.db) as acquired:
            self.assertTrue(acquired)
            with patch('queue_bridge.QueueBridge') as factory:
                self.assertFalse(app.reconcile_pending_shutdown(self.home))
        factory.assert_not_called()
        self.assertTrue(queue_state.pending(self.db))

    def test_confirmed_receipt_cleanup_permits_final_exit(self):
        row = self.dispatch()
        turn = str(uuid.uuid4())
        bridge = Mock()
        bridge.receipt.return_value = {'delivery': 'reached', 'turn': {'id': turn}}
        with patch('queue_bridge.QueueBridge') as factory:
            factory.return_value.__enter__.return_value = bridge
            self.assertTrue(app.reconcile_pending_shutdown(self.home))
        bridge.cancel_pending.assert_not_called()
        self.assertEqual(queue_state.pending(self.db), [])
        self.assertFalse(auto_watch.status(self.db)['enabled'])
        self.assertEqual(self.db.execute('SELECT resume_turn FROM attempts WHERE attempt_id=?',
                                         (row['attempt_id'],)).fetchone()[0], turn)
