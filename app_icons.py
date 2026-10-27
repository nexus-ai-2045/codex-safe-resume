"""常駐と配布アイコンの共通描画。画面ライブラリを読み込まない。"""
from pathlib import Path


def create_tray_image(Image, ImageDraw):
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    painter = ImageDraw.Draw(image)
    painter.rounded_rectangle((5, 5, 59, 59), radius=15, fill='#245A91')
    painter.arc((17, 16, 48, 48), 40, 310, fill='white', width=6)
    painter.polygon([(48, 46), (47, 30), (35, 39)], fill='white')
    return image


def generate_icons(output):
    """同じ絵柄からMac・Windows用の配布資産を生成する。"""
    from PIL import Image, ImageDraw
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    image = create_tray_image(Image, ImageDraw).resize((1024, 1024), Image.Resampling.LANCZOS)
    paths = {suffix: output / ('SafeResume.' + suffix) for suffix in ('png', 'icns', 'ico')}
    image.save(paths['png'], format='PNG')
    image.save(paths['icns'], format='ICNS')
    image.save(paths['ico'], format='ICO', sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    return paths
