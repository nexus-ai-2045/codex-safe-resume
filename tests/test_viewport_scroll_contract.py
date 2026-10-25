"""画面の要求寸法と確定寸法が異なる間も、同じスクロール座標を使う。"""
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from app import ScrollViewport


class ViewportScrollContract(unittest.TestCase):
    def viewport(self):
        view = ScrollViewport.__new__(ScrollViewport)
        view.canvas = Mock()
        view.canvas.winfo_width.return_value = 100
        view.canvas.winfo_height.return_value = 420
        view.canvas.canvasy.return_value = 0
        view.canvas.cget.return_value = '0 0 100 1000'
        view.canvas.tk.splitlist.side_effect = lambda text: text.split()
        view.body = SimpleNamespace(master=None, winfo_rooty=lambda: 0,
                                    winfo_height=lambda: 1200, winfo_reqheight=lambda: 1000)
        view.content = 1
        view.layout_size = None
        view.focus_update = None
        view.focus_target = None
        view.fresh_focus = False
        view.focus_anchor = None
        return view

    def test_focus_uses_scroll_region_extent_during_deferred_geometry(self):
        view = self.viewport()
        target = SimpleNamespace(master=view.body, winfo_exists=lambda: True,
                                 winfo_rooty=lambda: 800, winfo_height=lambda: 100)
        view.ensure_visible(target)
        view.canvas.yview_moveto.assert_called_once_with((900 - 420) / 1000)

    def test_focus_rechecks_after_idle_even_without_resize(self):
        view = self.viewport()
        view.ensure_visible = Mock()
        target = object()
        view.focus_changed(SimpleNamespace(widget=target))
        view.canvas.after.assert_called_once_with(30, view.restore_focus_visibility)
        view.restore_focus_visibility()
        self.assertEqual(view.ensure_visible.call_count, 2)

    def test_bottom_stays_at_bottom_when_wrapping_changes_extent(self):
        view = self.viewport()
        view.canvas.yview.return_value = (0.58, 1.0)
        view.focus_target = object()
        view.focus_update = 'old-focus'
        view.resize()
        view.canvas.after_cancel.assert_called_once_with('old-focus')
        view.canvas.yview_moveto.assert_called_once_with(1)
        self.assertIsNone(view.focus_update)

    def test_scrolled_up_view_is_not_forced_to_bottom(self):
        view = self.viewport()
        view.canvas.yview.return_value = (0.1, 0.52)
        view.resize()
        view.canvas.yview_moveto.assert_not_called()

    def test_fresh_focus_is_preserved_during_bottom_resize(self):
        view = self.viewport()
        view.canvas.yview.return_value = (0.58, 1.0)
        view.focus_target = object()
        view.focus_update = 'fresh-focus'
        view.fresh_focus = True
        view.resize()
        view.canvas.after_cancel.assert_not_called()
        self.assertEqual(view.focus_update, 'fresh-focus')

    def test_scroll_region_covers_native_body_and_shrinks_when_it_settles(self):
        view = self.viewport()
        view.canvas.winfo_height.return_value = 700
        view.canvas.yview.return_value = (0.0, 1.0)
        view.body.winfo_reqheight = lambda: 626
        view.body.winfo_height = lambda: 721
        view.resize()
        view.canvas.itemconfigure.assert_called_with(1, width=100, height=700)
        view.canvas.configure.assert_called_with(scrollregion=(0, 0, 100, 721))
        view.body.winfo_height = lambda: 700
        view.resize()
        view.canvas.configure.assert_called_with(scrollregion=(0, 0, 100, 700))

    def test_manual_scroll_clears_old_focus_before_deferred_native_resize(self):
        view = self.viewport()
        view.focus_target = object()
        view.focus_anchor = 200
        view.focus_update = 'stale-focus'
        view.canvas.canvasy.return_value = 0
        view.canvas.yview.return_value = (0.0, 0.6)
        view.resize()
        self.assertIsNone(view.focus_target)
        view.canvas.after_cancel.assert_called_once_with('stale-focus')
        view.canvas.after.assert_not_called()
        view.canvas.yview_moveto.assert_not_called()

    def test_pending_callback_respects_manual_scroll_without_configure(self):
        view = self.viewport()
        view.focus_target = object()
        view.focus_anchor = 200
        view.canvas.canvasy.return_value = 0
        view.ensure_visible = Mock()
        view.restore_focus_visibility()
        view.ensure_visible.assert_not_called()
        self.assertIsNone(view.focus_target)
