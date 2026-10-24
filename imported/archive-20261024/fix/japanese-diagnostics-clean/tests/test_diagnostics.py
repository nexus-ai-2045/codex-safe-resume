"""監視状況の日本語表示と情報漏えい防止のテスト。"""
import unittest
from diagnostics import monitor_status

class MonitorStatusTests(unittest.TestCase):
    def test_known_statuses(self):
        self.assertEqual(monitor_status({'state':'quota-wait'}), '利用枠の解除を待っています')
        self.assertEqual(monitor_status({'state':'offline'}), 'Codexへの接続を確認できません')
        self.assertEqual(monitor_status({'state':'unavailable'}), 'Codexの会話データを読み取れません')
        self.assertEqual(monitor_status({'state':'checked','results':[]}), '監視は正常です。今回の再開対象はありません')
        self.assertEqual(monitor_status({'state':'checked','results':[('test','dispatched')]}), '再開要求を送信しました（1件）。結果の確認が必要です')
    def test_never_disclose_raw_errors_or_paths(self):
        for state in ('offline','monitor-error','unavailable','unknown'):
            result={'state':state,'reason':'SECRET_MARKER_PATH','detail':'token=verysecret'}
            message=monitor_status(result)
            self.assertNotIn('SECRET_MARKER_PATH',message)
            self.assertNotIn('verysecret',message)
    def test_unknown_fails_closed(self):
        self.assertEqual(monitor_status({'state':'surprise'}), '監視状態を判定できません。自動再開せず確認が必要です')

if __name__=='__main__': unittest.main()
