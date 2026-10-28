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


def _intersects_cutout(button_frame, screen):
    try:
        if screen.safeAreaInsets().top <= 0:
            return False
        left = _rectangle(screen.auxiliaryTopLeftArea())
        right = _rectangle(screen.auxiliaryTopRightArea())
        if left[2] <= 0 or left[3] <= 0 or right[2] <= 0 or right[3] <= 0:
            return None
        x = left[0] + left[2]
        y = max(left[1], right[1])
        gap = (x, y, right[0] - x, min(left[1] + left[3], right[1] + right[3]) - y)
        return bool(_intersects(button_frame, gap))
    except Exception:
        # 古いOSの未対応APIや任意診断の失敗で基本診断を失敗させない。
        return None


def snapshot(icon):
    """メインスレッドから呼ぶ読取り専用診断。位置や識別情報は返さない。

    ノッチとの重なりを確認した配置はobscuredとする。presentedも
    他アイテムの重なりを含む実画面での視認までは保証しない。
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
        button_frame = None
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
        cutout_overlap = _intersects_cutout(button_frame, screen)
        status = 'presented' if presented else 'unplaced'
        if presented and cutout_overlap is True:
            status = 'obscured'
        return {
            'status': status,
            'native_visible': native_visible,
            'button_hidden': button_hidden,
            'image_valid': image_valid,
            'window_exists': window_exists,
            'window_visible': window_visible,
            'screen_intersection': screen_intersection,
            'menubar_intersection': menubar_intersection,
            'button_intersects_cutout_gap': cutout_overlap,
        }
    except Exception:
        return {'status': 'unknown'}
