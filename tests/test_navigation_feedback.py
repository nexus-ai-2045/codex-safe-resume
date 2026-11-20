"""チャット移動の結果は、画面を塞がずに表示する。"""
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
from tests import test_gui_controls as controls


class NavigationFeedback(unittest.TestCase):
    def gui(self):
        gui = controls.GuiControls().gui()
        controls.GuiControls().build(gui)
        gui.selected = Mock(return_value='selected-thread')
        gui.navigation_pending = False
        gui.navigation_thread = None
        gui.write = Mock()
        return gui

    def complete(self, gui, result='requested', error=None):
        navigator = SimpleNamespace(open_thread=Mock(return_value=result, side_effect=error))
        with patch('app.request_chat_open', navigator.open_thread), patch('app.messagebox') as dialogs:
            gui.open_selected_chat()
            gui.navigation_thread.join(1)
            self.assertFalse(gui.navigation_thread.is_alive())
            gui.drain_navigation()
        return navigator, dialogs

    def test_terminal_results_restore_controls_and_show_inline_feedback(self):
        for result, fragment in [('requested', '要求'), ('unavailable', '利用できません'),
                                 ('timeout', '時間'), ('cancelled', '取り消'), ('failed', '確認できません')]:
            with self.subTest(result=result):
                gui = self.gui()
                _, dialogs = self.complete(gui, result)
                self.assertFalse(gui.navigation_pending)
                gui.navigation_button.configure.assert_called_with(state='normal')
                self.assertIn(fragment, gui.status.set.call_args.args[0])
                dialogs.showwarning.assert_not_called()

    def test_unexpected_failure_is_inline_and_does_not_disclose_details(self):
        gui = self.gui()
        _, dialogs = self.complete(gui, error=RuntimeError('private native failure'))
        self.assertIn('確認できません', gui.status.set.call_args.args[0])
        self.assertNotIn('private native failure', gui.status.set.call_args.args[0])
        dialogs.showwarning.assert_not_called()

    def test_missing_selection_does_not_open_a_modal_or_start_a_worker(self):
        gui = self.gui()
        gui.selected.return_value = None
        with patch('app.messagebox') as dialogs:
            gui.open_selected_chat()
        dialogs.showinfo.assert_not_called()
        self.assertIn('選', gui.status.set.call_args.args[0])
        self.assertFalse(gui.navigation_pending)

    def test_mac_deadline_starts_before_the_worker(self):
        gui = self.gui()
        with patch('app.sys.platform', 'darwin'), patch('app.time.monotonic', return_value=100):
            navigator, _ = self.complete(gui)
        self.assertEqual(navigator.open_thread.call_args.kwargs['deadline'], 103)

    def test_common_button_inventory_does_not_depend_on_the_os(self):
        inventories = []
        for platform in ('darwin', 'win32'):
            gui = controls.GuiControls().gui()
            with patch('app.sys.platform', platform):
                records = controls.GuiControls().build(gui)
            inventories.append([(kind, options.get('text')) for kind, _, _, options in records
                                if kind in ('Button', 'Menubutton', 'Checkbutton', 'Radiobutton')])
        self.assertEqual(*inventories)
        self.assertIn(('Button', 'ChatGPTで開く'), inventories[0])

    def test_periodic_count_refresh_keeps_the_navigation_result(self):
        gui = self.gui()
        self.complete(gui, 'timeout')
        gui.simple_mode = True
        gui.update_status(7)
        self.assertIn('時間', gui.status.set.call_args.args[0])
        self.assertIn('7', gui.status.set.call_args.args[0])

    def test_old_poll_callback_does_not_touch_the_new_request(self):
        gui = self.gui()
        gui.navigation_generation = 2
        gui.navigation_pending = True
        gui.navigation_button.reset_mock()
        gui.drain_navigation(1)
        gui.navigation_button.configure.assert_not_called()
        self.assertTrue(gui.navigation_pending)
