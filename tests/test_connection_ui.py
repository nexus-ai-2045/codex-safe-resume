"""設定済みの監視を、検索・絞込み後に送信停止と誤表示しない。"""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import core
from connection_settings import configure


class ConnectionUi(unittest.TestCase):
    def test_render_keeps_configured_monitoring_status(self):
        with tempfile.TemporaryDirectory() as name, closing(core.connection(Path(name)/'state')) as db:
            cli = Path(name)/'cli'; cli.write_bytes(b'fixture'); cli.chmod(0o700)
            configure(db, cli, desktop_confirmed=True, manual_exclusion_acknowledged=True)
            gui = app.GUI.__new__(app.GUI)
            gui.db = db
            gui.rows = [dict(id='synthetic', title='デモ', cwd='', enabled=1, auto_excluded=0,
                             state='waiting', reset_at=None)]
            gui.known = [dict(id='synthetic', limited=False)]
            gui.search = Mock(); gui.search.get.return_value = ''
            gui.filter = Mock(); gui.filter.get.return_value = 'すべて'
            gui.selected = lambda: None
            gui.select_row = lambda: None
            gui.tree = Mock(); gui.tree.get_children.return_value = []
            gui.status = Mock()
            gui.render_rows()
            text = gui.status.set.call_args.args[0]
            self.assertIn('監視対象 1件', text)
            self.assertNotIn('送信停止', text)

    def test_without_config_clearly_displays_stop(self):
        with tempfile.TemporaryDirectory() as name, closing(core.connection(Path(name)/'state')) as db:
            gui = app.GUI.__new__(app.GUI)
            gui.db, gui.rows, gui.known = db, [], []
            gui.status = Mock()
            gui.update_status(0)
            self.assertIn('送信停止', gui.status.set.call_args.args[0])


class ReviewedResetUi(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = core.connection(Path(self.temp.name)/'state')
        self.addCleanup(self.db.close)
        self.tid = '123e4567-e89b-12d3-a456-426614174000'
        core.sync(self.db, [dict(id=self.tid, title='デモ', cwd='', limited=False)])
        core.mark(self.db, self.tid, 'blocked')
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.db = self.db
        self.gui.root = Mock()
        self.gui.selected = Mock(return_value=self.tid)
        self.gui.refresh = Mock()
        self.gui.write = Mock()

    def row(self):
        return core.list_threads(self.db)[0]

    def test_cancel_changes_nothing(self):
        before = self.row()
        changes = self.db.total_changes
        with patch('app.messagebox') as dialogs:
            dialogs.askyesno.return_value = False
            self.gui.review_reset()
            dialogs.askyesno.assert_called_once()
        self.assertEqual(self.row(), before)
        self.assertEqual(self.db.total_changes, changes)
        self.gui.refresh.assert_not_called()
        self.gui.write.assert_not_called()

    def test_confirmation_resets_but_does_not_enable(self):
        generation = self.row()['generation']
        with patch('app.messagebox') as dialogs:
            dialogs.askyesno.return_value = True
            self.gui.review_reset()
            message = dialogs.askyesno.call_args.args[1]
            self.assertIn('Desktop', message)
            self.assertIn('自動では有効', message)
            self.assertIn('同じ停止ターン', message)
            dialogs.showwarning.assert_not_called()
        self.assertEqual(self.row()['state'], 'idle')
        self.assertFalse(self.row()['enabled'])
        self.assertEqual(self.row()['generation'], generation + 1)
        self.gui.refresh.assert_called_once()

    def test_active_row_never_prompts_or_resets(self):
        self.db.execute("UPDATE threads SET state='monitoring',enabled=1")
        before = self.row()
        with patch('app.messagebox') as dialogs:
            self.gui.review_reset()
            dialogs.askyesno.assert_not_called()
            dialogs.showwarning.assert_called_once()
        self.assertEqual(self.row(), before)

    def test_state_change_during_confirmation_is_rejected(self):
        def changed(*args, **kwargs):
            self.db.execute("UPDATE threads SET state='claimed',enabled=1")
            return True
        with patch('app.messagebox') as dialogs:
            dialogs.askyesno.side_effect = changed
            self.gui.review_reset()
            dialogs.showwarning.assert_called_once()
        self.assertEqual(self.row()['state'], 'claimed')
        self.assertTrue(self.row()['enabled'])
        self.gui.write.assert_not_called()

    def test_generation_change_during_confirmation_preserves_row_and_attempts(self):
        self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                        (self.tid, 'failure', 'attempt', 1, 'sending', None))
        before = self.row()
        attempts = [tuple(row) for row in self.db.execute('SELECT * FROM attempts')]
        changes = self.db.total_changes
        def changed(*args, **kwargs):
            self.db.execute('UPDATE threads SET generation=generation+1')
            return True
        with patch('app.messagebox') as dialogs:
            dialogs.askyesno.side_effect = changed
            self.gui.review_reset()
            dialogs.showwarning.assert_called_once()
        self.assertEqual(self.row(), dict(before, generation=before['generation'] + 1))
        self.assertEqual(self.db.total_changes, changes + 1)
        self.assertEqual([tuple(row) for row in self.db.execute('SELECT * FROM attempts')], attempts)
        self.gui.write.assert_not_called()

    def test_review_menu_action_is_wired_without_opening_gui(self):
        for name in ('search', 'filter', 'details', 'status', 'tray_state', 'usage_status', 'auto_status', 'plan_note'):
            setattr(self.gui, name, Mock())
        self.gui.update_connection_note = Mock()
        self.gui.refresh_auto_status = Mock()
        with patch('app.tk') as widgets, patch('app.ttk'), patch('app.QuotaCards'):
            self.gui.build()
        matching = [call for call in widgets.Menu.return_value.add_command.call_args_list
                    if call.kwargs.get('label', '').startswith('確認して選び直す')]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].kwargs['command'], self.gui.review_reset)
