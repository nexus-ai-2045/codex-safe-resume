"""監視の日本語化と例外情報の非表示を検証する。"""
import unittest
from diagnostics import monitor_status
class MonitorStatusTests(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(monitor_status({'state':'quota-wait'}),'利用枠の解除を待っています')
        self.assertEqual(monitor_status({'state':'offline'}),'Codexへの接続を確認できません')
        self.assertEqual(monitor_status({'state':'checked','results':[]}), '監視は正常です。今回の再開対象はありません')
    def test_error_details_not_echoed(self):
        for state in ('offline','monitor-error','unavailable','unknown'):
            message=monitor_status({'state':state,'reason':'秘密の例外本文XYZ'})
            self.assertNotIn('秘密の例外本文XYZ',message)
    def test_unknown_state(self):
        self.assertEqual(monitor_status({'state':'surprise'}),'監視状態を判定できません。自動再開せず確認が必要です')
