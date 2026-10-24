import unittest
from pathlib import Path
from app import connection_text
from desktop_bridge import DesktopBridge

class GuiText(unittest.TestCase):
    def test_ソースと文書と配布設定に旧メニュー名を残さない(self):
        root = Path(__file__).resolve().parents[1]
        old_label = 'その他の' + '操作'
        ignored = {'__pycache__', 'build', 'dist', 'release'}
        checked = []
        for path in root.rglob('*'):
            parts = path.relative_to(root).parts
            if any(part.startswith('.') or part in ignored for part in parts):
                continue
            if path.is_file() and (path.suffix in ('.py', '.md') or parts[0] == 'packaging'):
                with self.subTest(file=str(path.relative_to(root))):
                    self.assertNotIn(old_label, path.read_text(encoding='utf-8'),
                                     'メニューへの案内は「設定」に合わせてください')
                checked.append(path)
        self.assertIn(root / 'app.py', checked)
        self.assertIn(root / 'docs' / '接続設定.md', checked)
        self.assertTrue(any(path.parent == root / 'tests' for path in checked))
        self.assertTrue(any('packaging' in path.parts for path in checked))

    def test_displays_unverified_transport_hold(self):
        content = connection_text({})
        self.assertFalse(DesktopBridge().capabilities()['safe_admission'])
        self.assertIn('Desktop本人確認: 未', content)
        self.assertIn('共有接続: 未設定', content)
        self.assertIn('アカウント: 未照合', content)
        self.assertIn('自動送信: 停止', content)
        self.assertNotIn('読取り成功', content)

if __name__=='__main__': unittest.main()
