"""監視結果の全状態を列挙し、画面・ログ・受領の期限を検証する。"""
import ast
from contextlib import closing
import inspect
from pathlib import Path
import queue
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

import app
import auto_watch
import core
from diagnostics import monitor_status
import watch


# 返却元の状態集合と照合する。状態を増やしたら表示と期待値も点検する。
STATES = {
    'checked': ('監視中：利用枠で止まったチャットを確認しています',
                '監視は正常です。今回の再開対象はありません'),
    'empty': ('監視中：対象のチャットはまだありません',
              '検出されたチャットがありません'),
    'quota-wait': ('監視中：利用枠の回復を待っています',
                   '利用枠の解除を待っています'),
    'disabled': ('停止中：自動再開は無効です',
                 '自動再開は無効のため、監視を停止しています'),
    'busy': ('接続確認中：別の監視処理が確認しています',
             '別の監視処理が確認中です'),
    'offline': ('要確認：監視結果を確認できません。接続を確認してください',
                'Codexへの接続を確認できません'),
    'unavailable': ('要確認：監視結果を確認できません。接続を確認してください',
                    'Codexの会話データを読み取れません'),
    'unsupported-transport': ('要確認：監視結果を確認できません。接続を確認してください',
                              '安全なDesktop送信経路が未検証のため、自動送信を停止しています'),
    'monitor-error': ('要確認：監視結果を確認できません。接続を確認してください',
                      '監視中にエラーが発生しました。次回再確認します'),
}


class MonitorStateDisplay(unittest.TestCase):
    def setUp(self):
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.known = []
        self.gui.rows = []
        self.gui.auto_status = Mock()
        self.gui.auto_button = Mock()
        self.gui.connection_action = Mock()
        self.policy = {'enabled': True, 'generation': 2}
        self.config = {'mode': 'managed', '_runtime_attested': True}
        clock = patch('app.time.monotonic', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def receipt(self, state):
        self.gui.last_monitor_result = {'state': state, 'results': []}
        self.gui.last_monitor_policy = dict(self.policy)
        self.gui.last_monitor_config = dict(self.config)
        self.gui.last_monitor_at = 1000

    def render(self, *, ready=True):
        self.gui.connection_action.reset_mock()
        self.gui.render_auto_status(self.policy, self.config, ready)
        return self.gui.auto_status.set.call_args.args[0]

    def test_返却元の状態が増減したら表示対応表の更新を要求する(self):
        sources = [inspect.getsource(watch), inspect.getsource(auto_watch),
                   textwrap.dedent(inspect.getsource(app.GUI.monitor_tick))]
        states = set()
        for source in sources:
            for node in ast.walk(ast.parse(source)):
                values = []
                if isinstance(node, ast.Dict):
                    values = [value for key, value in zip(node.keys, node.values)
                              if isinstance(key, ast.Constant) and key.value == 'state']
                elif isinstance(node, ast.Call):
                    values = [arg.value for arg in node.keywords if arg.arg == 'state']
                for value in values:
                    self.assertIsInstance(value, ast.Constant,
                                          '動的な監視状態の返却を追加した場合も列挙を更新してください')
                    self.assertIsInstance(value.value, str)
                    states.add(value.value)
        self.assertEqual(states, set(STATES), '監視状態の画面・ログ・文書を更新してください')

    def test_全状態の新しい受領を意図した画面と操作へ割り当てる(self):
        for state, (expected, _) in STATES.items():
            with self.subTest(state=state):
                self.receipt(state)
                with patch('app.monitor_once') as monitor, \
                        patch('app.connection_enabled') as attest, \
                        patch('app.connection_options') as config:
                    self.assertEqual(self.render(), expected)
                monitor.assert_not_called()
                attest.assert_not_called()
                config.assert_not_called()
                action = self.gui.connection_action
                self.assertEqual(action.configure.call_args.kwargs['command'],
                                 self.gui.reconnect_auto_watch)
                if expected.startswith('要確認'):
                    action.grid.assert_called_once()
                    action.grid_remove.assert_not_called()
                else:
                    action.grid_remove.assert_called_once()
                    action.grid.assert_not_called()

    def test_全状態のログを日本語で明示する(self):
        for state, (_, expected) in STATES.items():
            with self.subTest(state=state):
                self.assertEqual(monitor_status({'state': state}), expected)

    def test_未知と欠落した状態の新しい受領は要確認にする(self):
        for result in ({'state': '追加された状態'}, {'state': None}, {},
                       {'state': 123}, {'state': []}, {'state': {}}):
            with self.subTest(result=result):
                self.receipt(None)
                self.gui.last_monitor_result = result
                self.assertEqual(self.render(), '要確認：監視結果を確認できません')
                self.gui.connection_action.grid.assert_called_once()
                self.assertEqual(monitor_status(result),
                                 '監視状態を判定できません。自動再開せず確認が必要です')

    def test_全状態で受領の期限と方針と接続設定の一致を必須にする(self):
        for state in (*STATES, '追加された状態'):
            for change in ('期限切れ', '未来の時刻', '方針変更', '接続変更'):
                with self.subTest(state=state, change=change):
                    self.receipt(state)
                    if change == '期限切れ':
                        self.gui.last_monitor_at = 909
                    elif change == '未来の時刻':
                        self.gui.last_monitor_at = 1001
                    elif change == '方針変更':
                        self.gui.last_monitor_policy['generation'] += 1
                    else:
                        self.gui.last_monitor_config['launch_nonce'] = '別の接続'
                    self.assertEqual(self.render(), '接続確認中：最新の監視結果を確認しています')
                    self.gui.connection_action.grid_remove.assert_called_once()

    def test_受領前は最新の結果を確認中と表示する(self):
        self.assertEqual(self.render(), '接続確認中：最新の監視結果を確認しています')

    def test_全状態より現在の停止と要確認行と接続の失敗を優先する(self):
        for state in STATES:
            with self.subTest(state=state):
                self.receipt(state)
                self.policy['enabled'] = False
                self.assertEqual(self.render(), '停止中：自動再開は無効です')
                self.policy['enabled'] = True
                self.gui.known = [{'id': '対象'}]
                self.gui.rows = [{'id': '対象', 'state': 'blocked'}]
                self.assertEqual(self.render(),
                                 '要確認：停止した対象があります。要確認のチャットを確認してください')
                self.assertEqual(self.gui.connection_action.configure.call_args.kwargs['command'],
                                 self.gui.show_review_targets)
                self.gui.known, self.gui.rows = [], []
                self.config['_connection_phase'] = 'failed'
                self.assertEqual(self.render(),
                                 '要確認：接続できません。接続の確認・修復を行ってください')
                del self.config['_connection_phase']
                self.assertEqual(self.render(ready=False),
                                 '接続確認中：Desktopとの接続を準備しています')

    def test_照合結果の送信済みと要確認をログに反映する(self):
        for results, expected in (
                ([('対象', 'dispatched')], '再開要求を送信しました（1件）。結果の確認が必要です'),
                ([('対象', 'blocked')],
                 '再開を安全に確認できないチャットがあります（1件）。手動確認が必要です'),
                ([('対象', 'dispatched'), ('別の対象', 'blocked')],
                 '再開を安全に確認できないチャットがあります（1件）。手動確認が必要です')):
            with self.subTest(results=results):
                self.assertEqual(monitor_status({'state': 'checked', 'results': results}), expected)

    def test_対象なしの実際の走査結果を監視中として受領し状態を書き換えない(self):
        with tempfile.TemporaryDirectory() as folder, \
                closing(core.connection(Path(folder) / '状態')) as db:
            self.policy = auto_watch.initialize(db)
            self.policy = auto_watch.set_enabled(db, True)
            self.gui.db = db
            self.gui.closing = False
            self.gui.reload_rows = Mock()
            self.gui.render_rows = lambda: self.render()
            self.gui.write = Mock()
            factory = Mock(side_effect=AssertionError('対象なしでは接続しません'))
            before = db.total_changes
            with patch('watch.discover', return_value=([], None)):
                result = watch.once(db, factory)
            self.assertEqual(result, {'state': 'empty'})
            factory.assert_not_called()
            result.update(_ui_policy=dict(self.policy), _ui_config=dict(self.config),
                          _ui_finished_at=1000, _ui_revision=0)
            self.gui.monitor_result(result)
            self.assertEqual(self.gui.auto_status.set.call_args.args[0], STATES['empty'][0])
            self.gui.write.assert_called_once_with(STATES['empty'][1])
            self.assertEqual(db.total_changes, before)
            self.assertEqual(core.list_threads(db), [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)

    def test_監視処理の例外を監視エラーとして受領して要確認にする(self):
        self.gui.closing = False
        self.gui.db = Mock()
        self.gui.root = Mock()
        self.gui.monitor_running = False
        self.gui.monitor_queue = queue.Queue()
        self.gui.refresh_auto_status = Mock()
        self.gui.reload_rows = Mock()
        self.gui.render_rows = lambda: self.render()
        self.gui.write = Mock()
        with patch('app.auto_watch_status', return_value=self.policy), \
                patch('app.connection_options', return_value=self.config), \
                patch('app.connection', side_effect=RuntimeError('非公開の例外詳細')), \
                patch('app.threading.Thread') as worker:
            worker.side_effect = lambda *, target, daemon, **kwargs: Mock(start=target)
            self.gui.monitor_tick()
        self.gui.drain_monitor()
        self.assertEqual(self.gui.last_monitor_result['state'], 'monitor-error')
        self.assertEqual(self.gui.auto_status.set.call_args.args[0], STATES['monitor-error'][0])
        self.gui.write.assert_called_once_with(STATES['monitor-error'][1])
        self.assertFalse(self.gui.monitor_running)


if __name__ == '__main__':
    unittest.main()
