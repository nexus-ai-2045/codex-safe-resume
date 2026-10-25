import tkinter as tk
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import app


class SmallWindowLayout(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        # Keep real mapped Tk geometry, without placing test windows over user work.
        if getattr(self, "tk_scaling", None) is not None:
            previous_scaling = self.root.tk.call("tk", "scaling")
            self.root.tk.call("tk", "scaling", self.tk_scaling)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes('-alpha', 0.0)
        self.addCleanup(self.root.destroy)
        if getattr(self, "tk_scaling", None) is not None:
            self.addCleanup(self.root.tk.call, "tk", "scaling", previous_scaling)
        self.root.title('Safe Resume layout regression')
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.root = self.root
        self.gui.simple_mode = True
        self.gui.rows, self.gui.known = [], []
        for name in ('search', 'filter', 'details', 'status', 'tray_state',
                     'usage_status', 'auto_status', 'plan_note'):
            setattr(self.gui, name, tk.StringVar(self.root, value='表示確認 ' * 12))
        self.gui.update_connection_note = Mock()
        self.gui.refresh_auto_status = Mock()
        self.gui.render_usage = Mock()
        self.gui.render_rows = Mock()
        self.gui.configure_style()
        self.gui.build()
        self.gui.quota_cards.render(dict(stale=True, cards=[
            dict(key=key, name=name, summary='残り50%・取得済み',
                 reset_text='更新待ち・回復予定を確認しています', remaining_percent=50)
            for key, name in (('primary', '5時間枠'), ('secondary', '1週間枠'))]))

    def settle(self, width, height):
        x, y = self.root.winfo_screenwidth() + 100, self.root.winfo_screenheight() + 100
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        if self.root.state() == 'withdrawn':
            self.root.deiconify()
        self.flush()

    def flush(self):
        previous, stable = None, 0
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.after(30, self.root.quit)
            self.root.mainloop()
            viewport = getattr(self.gui, 'viewport', None)
            state = (self.root.winfo_width(), self.root.winfo_height(),
                     self.gui.target_toggle.winfo_rooty(),
                     self.gui.target_toggle.master.winfo_reqheight(),
                     viewport.body.winfo_height() if viewport else None,
                     viewport.body.winfo_reqheight() if viewport else None,
                     viewport.canvas.yview() if viewport else None)
            stable = stable + 1 if state == previous else 0
            if stable >= 3:
                return
            previous = state
        self.fail(f'Layout did not settle: {state}')

    def assert_visible(self, widget):
        viewport = getattr(self.gui, 'viewport', None)
        bounds = viewport.canvas if viewport else self.root
        top = widget.winfo_rooty() - bounds.winfo_rooty()
        self.assertGreaterEqual(top, -1)
        self.assertLessEqual(top + widget.winfo_height(), bounds.winfo_height() + 1)

    def test_small_windows_keep_top_and_bottom_controls_reachable(self):
        for width, height in ((620, 420), (752, 500), (1060, 700)):
            for details in (False, True):
                with self.subTest(width=width, height=height, details=details):
                    for widget in self.gui.advanced_widgets:
                        widget.grid() if details else widget.grid_remove()
                    self.gui.connection_action.grid()
                    self.settle(width, height)
                    cards = self.gui.quota_cards
                    if cards.columns == 1:
                        for item in cards.items:
                            self.assertEqual(item['frame'].winfo_width(), cards.winfo_width())
                    viewport = getattr(self.gui, 'viewport', None)
                    if viewport:
                        viewport.canvas.yview_moveto(0)
                        self.flush()
                    self.assert_visible(self.gui.auto_button)
                    if viewport:
                        viewport.canvas.yview_moveto(1)
                        self.flush()
                    self.assert_visible(self.gui.target_toggle)
                    self.assert_visible(self.gui.navigation_button)
                    for control in (self.gui.target_toggle, self.gui.navigation_button):
                        left = control.winfo_rootx() - viewport.canvas.winfo_rootx()
                        self.assertGreaterEqual(left, 0)
                        self.assertLessEqual(left + control.winfo_width(), viewport.canvas.winfo_width())
                    footer = self.gui.target_toggle.master.master.grid_slaves(row=6)[0]
                    self.assert_visible(footer)

    def test_resize_restores_space_and_keeps_table_scrollbars(self):
        self.settle(620, 420)
        viewport = getattr(self.gui, 'viewport', None)
        self.assertIsNotNone(viewport)
        self.assertLess(viewport.canvas.yview()[1], 1)
        for name in ('details', 'status', 'tray_state', 'auto_status', 'plan_note'):
            getattr(self.gui, name).set('表示確認')
        self.settle(1060, 700)
        self.settle(1060, max(700, viewport.body.winfo_reqheight() + 20))
        self.assertEqual(viewport.canvas.yview(), (0.0, 1.0))
        self.assertTrue(self.gui.tree.cget('xscrollcommand'))
        self.assertTrue(self.gui.tree.cget('yscrollcommand'))
        self.settle(620, 420)
        for index in range(50):
            self.gui.tree.insert('', 'end', values=(str(index), '', '', '', ''))
        self.gui.tree.column('title', minwidth=1000, width=1000)
        self.flush()
        self.gui.tree.yview_moveto(1)
        self.gui.tree.xview_moveto(1)
        self.assertGreater(self.gui.tree.yview()[0], 0)
        self.assertGreater(self.gui.tree.xview()[0], 0)
        self.gui.target_toggle.event_generate('<FocusIn>')
        self.flush()
        self.assert_visible(self.gui.target_toggle)
        self.gui.viewport.canvas.yview_moveto(0)
        self.flush()
        self.assertEqual(self.gui.viewport.canvas.yview()[0], 0)

    def test_pending_focus_update_ignores_destroyed_control(self):
        self.settle(620, 420)
        failures = []
        self.root.report_callback_exception = lambda *args: failures.append(args)
        control = tk.Button(self.gui.viewport.body, text='試験用')
        self.gui.viewport.focus_target = control
        self.gui.viewport.layout_size = None
        self.gui.viewport.resize()
        control.destroy()
        self.flush()
        self.assertEqual(failures, [])

    def test_vertical_resize_keeps_focused_control_visible(self):
        self.settle(620, 620)
        self.gui.target_toggle.event_generate('<FocusIn>')
        self.flush()
        self.assert_visible(self.gui.target_toggle)
        self.settle(620, 420)
        self.assert_visible(self.gui.target_toggle)

    def test_pending_update_uses_current_focus(self):
        self.settle(620, 420)
        viewport = self.gui.viewport
        viewport.ensure_visible = Mock()
        viewport.focus_target = self.gui.auto_button
        viewport.layout_size = None
        viewport.resize()
        viewport.focus_changed(SimpleNamespace(widget=self.gui.target_toggle))
        self.flush()
        self.assertEqual(viewport.ensure_visible.call_args.args,
                         (self.gui.target_toggle,))

class ScaledSmallWindowLayout(SmallWindowLayout):
    tk_scaling = 1.5
