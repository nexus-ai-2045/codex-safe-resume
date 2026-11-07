"""画面を開かず、実際の初期化処理が監視タイマーを登録することを確認。"""
import ast
import contextlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import app
from app import usage_unknown_text


class Startup(unittest.TestCase):
    def test_monitor_and_result_drain_start_once(self):
        source = ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        gui = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'GUI')
        init = next(n for n in gui.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        scope = {'tk': SimpleNamespace(Tk=Mock(return_value=Mock()), StringVar=Mock),
                 'connection': Mock, 'queue': __import__('queue'), 'threading': __import__('threading'),
                 'usage_unknown_text': usage_unknown_text}
        exec(compile(ast.Module(body=[init], type_ignores=[]), 'app.py', 'exec'), scope)
        instance = Mock()
        scope['__init__'](instance, False)
        instance.show_window.assert_called_once()
        instance.configure_reopen.assert_called_once()
        callbacks = [call.args[1] for call in instance.root.after.call_args_list]
        self.assertEqual(callbacks.count(instance.monitor_tick), 1)
        self.assertEqual(callbacks.count(instance.drain_monitor), 1)
        self.assertEqual(callbacks.count(instance.periodic), 1)

class QuitFailure(unittest.TestCase):
    def test_tray_stop_failure_is_not_reported_as_exit(self):
        instance = app.GUI.__new__(app.GUI)
        instance.root, instance.db, instance.tray = Mock(), Mock(), Mock()
        instance.write, instance.show_window = Mock(), Mock()
        instance.closing = False
        instance.tray.stop.side_effect = RuntimeError('private-error')
        with patch('app.set_auto_watch'), patch('queue_state.pending', return_value=[]):
            instance.quit_app()
        instance.db.close.assert_not_called()
        instance.root.destroy.assert_not_called()
        self.assertFalse(instance.closing)

class DefaultLaunch(unittest.TestCase):
    def test_no_arguments_launches_gui(self):
        source=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text(encoding='utf-8'))
        main=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        from diagnostics import JapaneseParser
        gui=Mock()
        scope={'sys':SimpleNamespace(argv=['CodexSafeResume']), 'JapaneseParser':JapaneseParser,'GUI':gui,
               'gui_lease': lambda: contextlib.nullcontext(True)}
        exec(compile(ast.Module(body=[main],type_ignores=[]),'app.py','exec'),scope)
        scope['main']()
        gui.assert_called_once_with(enable_tray=True)
        gui.return_value.run.assert_called_once()

if __name__ == '__main__': unittest.main()
