"""配布画像の実読戻し。Tkやウィンドウを使わない。"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw

from app_icons import create_tray_image, generate_icons


class AppIcons(unittest.TestCase):
    def test_cli_writes_utf8_with_a_windows_output_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(__file__).resolve().parents[1] / 'packaging/build-icons.py'
            env = dict(os.environ, PYTHONIOENCODING='cp1252')
            result = subprocess.run([sys.executable, str(script), '--output', directory],
                                    env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0)
            self.assertIn('共通アイコンを生成しました', result.stdout.decode('utf-8'))

    def test_mac_and_png_use_the_same_tray_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = generate_icons(directory)
            expected = create_tray_image(Image, ImageDraw).resize(
                (1024, 1024), Image.Resampling.LANCZOS)
            for suffix in ('png', 'icns'):
                with Image.open(paths[suffix]) as image:
                    self.assertEqual(image.size, expected.size)
                    self.assertEqual(image.convert('RGBA').tobytes(), expected.tobytes())

    def test_windows_icon_contains_small_and_large_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = generate_icons(directory)
            with Image.open(paths['ico']) as image:
                self.assertIn((16, 16), image.info['sizes'])
                self.assertIn((256, 256), image.info['sizes'])
                self.assertEqual(image.size, (256, 256))
            self.assertEqual({path.name for path in Path(directory).iterdir()},
                             {'SafeResume.png', 'SafeResume.icns', 'SafeResume.ico'})
