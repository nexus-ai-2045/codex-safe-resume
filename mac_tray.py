"""Mac常駐アイコンのnative配置を読み取る。画面上の視認は別途確認する。"""
import math
import sys


def _intersects(first, second):
    x, y, width, height = first
    other_x, other_y, other_width, other_height = second
    return (width > 0 and height > 0 and other_width > 0 and other_height > 0
            and max(x, other_x) < min(x + width, other_x + other_width)
            and max(y, other_y) < min(y + height, other_y + other_height))


def _rectangle(frame):
    return (float(frame.origin.x), float(frame.origin.y),
            float(frame.size.width), float(frame.size.height))


def snapshot(icon):
    """メインスレッドから呼ぶ読取り専用診断。位置や識別情報は返さない。

    presentedはnativeの配置条件を満たす意味であり、他アイテムの重なりや
    ノッチによる遮蔽を含む実画面での視認までは保証しない。
    """
    if sys.platform != 'darwin' or icon is None:
        return {'status': 'unavailable'}
    try:
        item = getattr(icon, '_status_item', None)
        if item is None:
            return {'status': 'unavailable'}
        import AppKit

        native_visible = bool(item.isVisible())
        button = item.button()
        if button is None:
            return {'status': 'unknown'}
        button_hidden = bool(button.isHidden())
        image = button.image()
        image_size = image.size() if image is not None else None
        image_valid = bool(image_size is not None
                           and 0 < image_size.width < math.inf
                           and 0 < image_size.height < math.inf)
        window = button.window()
        window_exists = window is not None
        window_visible = bool(window_exists and window.isVisible())
        screen_intersection = False
        menubar_intersection = False
        screen = window.screen() if window_exists else None
        if screen is not None:
            button_frame = _rectangle(window.convertRectToScreen_(
                button.convertRect_toView_(button.bounds(), None)))
            screen_frame = _rectangle(screen.frame())
            screen_intersection = bool(_intersects(button_frame, screen_frame))
            thickness = float(AppKit.NSStatusBar.systemStatusBar().thickness())
            x, y, width, height = screen_frame
            band = (x, y + height - thickness, width, thickness)
            menubar_intersection = bool(screen_intersection
                                        and 0 < thickness < math.inf
                                        and _intersects(button_frame, band))
        presented = (native_visible and not button_hidden and image_valid
                     and window_visible and menubar_intersection)
        return {
            'status': 'presented' if presented else 'unplaced',
            'native_visible': native_visible,
            'button_hidden': button_hidden,
            'image_valid': image_valid,
            'window_exists': window_exists,
            'window_visible': window_visible,
            'screen_intersection': screen_intersection,
            'menubar_intersection': menubar_intersection,
        }
    except Exception:
        return {'status': 'unknown'}
