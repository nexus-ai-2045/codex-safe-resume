"""画面を作らず、nativeの配置診断と返却情報の境界を確認する。"""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import mac_tray


def rect(x, y, width, height):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y),
                           size=SimpleNamespace(width=width, height=height))


class MacTraySnapshot(unittest.TestCase):
    def setUp(self):
        self.screen = Mock()
        self.screen.frame.return_value = rect(0, 0, 1440, 900)
        self.window = Mock()
        self.window.isVisible.return_value = True
        self.window.screen.return_value = self.screen
        self.window.convertRectToScreen_.return_value = rect(1000, 878, 24, 22)
        self.button = Mock()
        self.button.isHidden.return_value = False
        self.button.window.return_value = self.window
        self.button.image.return_value.size.return_value = SimpleNamespace(width=22, height=22)
        self.item = Mock()
        self.item.isVisible.return_value = True
        self.item.button.return_value = self.button
        self.icon = SimpleNamespace(_status_item=self.item)
        self.appkit = Mock()
        self.appkit.NSStatusBar.systemStatusBar.return_value.thickness.return_value = 24
        self.platform = patch.object(mac_tray.sys, 'platform', 'darwin')
        self.modules = patch.dict('sys.modules', {'AppKit': self.appkit})
        self.platform.start()
        self.modules.start()
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.modules.stop)

    def test_presented_has_only_json_safe_state_and_booleans(self):
        result = mac_tray.snapshot(self.icon)
        self.assertEqual(result, {
            'status': 'presented', 'native_visible': True, 'button_hidden': False,
            'image_valid': True, 'window_exists': True, 'window_visible': True,
            'screen_intersection': True, 'menubar_intersection': True,
        })
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_native_visibility_does_not_prove_screen_placement(self):
        # AppKit may supply a screen even for a fallback frame outside it.
        self.window.convertRectToScreen_.return_value = rect(2000, 878, 24, 22)
        result = mac_tray.snapshot(self.icon)
        self.assertEqual(result['status'], 'unplaced')
        self.assertTrue(result['native_visible'])
        self.assertFalse(result['screen_intersection'])
        self.assertFalse(result['menubar_intersection'])

    def test_below_screen_and_below_menubar_are_unplaced(self):
        for y, on_screen in ((-50, False), (400, True), (900, False)):
            with self.subTest(y=y):
                self.window.convertRectToScreen_.return_value = rect(1000, y, 24, 22)
                result = mac_tray.snapshot(self.icon)
                self.assertEqual(result['status'], 'unplaced')
                self.assertEqual(result['screen_intersection'], on_screen)
                self.assertFalse(result['menubar_intersection'])

    def test_zero_size_image_or_frame_is_unplaced(self):
        self.button.image.return_value.size.return_value.width = 0
        self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'unplaced')
        self.button.image.return_value.size.return_value.width = 22
        self.window.convertRectToScreen_.return_value = rect(1000, 878, 0, 22)
        self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'unplaced')

    def test_hidden_button_hidden_window_or_native_item_is_unplaced(self):
        for getter, value in ((self.button.isHidden, True),
                              (self.window.isVisible, False),
                              (self.item.isVisible, False)):
            with self.subTest(getter=getter):
                old = getter.return_value
                getter.return_value = value
                self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'unplaced')
                getter.return_value = old

    def test_missing_image_window_or_screen_is_unplaced(self):
        for getter in (self.button.image, self.button.window, self.window.screen):
            with self.subTest(getter=getter):
                old = getter.return_value
                getter.return_value = None
                self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'unplaced')
                getter.return_value = old

    def test_native_exception_is_unknown_and_does_not_leak_text(self):
        self.item.isVisible.side_effect = RuntimeError('private details')
        self.assertEqual(mac_tray.snapshot(self.icon), {'status': 'unknown'})

    def test_geometry_exception_is_unknown(self):
        self.window.convertRectToScreen_.side_effect = RuntimeError('private geometry')
        self.assertEqual(mac_tray.snapshot(self.icon), {'status': 'unknown'})

    def test_zero_size_screen_or_menu_band_is_unplaced(self):
        self.screen.frame.return_value = rect(0, 0, 1440, 0)
        self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'unplaced')
        self.screen.frame.return_value = rect(0, 0, 1440, 900)
        self.appkit.NSStatusBar.systemStatusBar.return_value.thickness.return_value = 0
        self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'unplaced')

    def test_missing_native_button_is_unknown(self):
        self.item.button.return_value = None
        self.assertEqual(mac_tray.snapshot(self.icon), {'status': 'unknown'})

    def test_unsupported_platform_and_absent_item_are_unavailable(self):
        with patch.object(mac_tray.sys, 'platform', 'win32'):
            self.assertEqual(mac_tray.snapshot(self.icon), {'status': 'unavailable'})
        self.assertEqual(mac_tray.snapshot(None), {'status': 'unavailable'})
        self.assertEqual(mac_tray.snapshot(SimpleNamespace()), {'status': 'unavailable'})

    def test_secondary_screen_origin_is_respected(self):
        self.screen.frame.return_value = rect(-1600, -200, 1600, 1000)
        self.window.convertRectToScreen_.return_value = rect(-400, 778, 24, 22)
        self.assertEqual(mac_tray.snapshot(self.icon)['status'], 'presented')
