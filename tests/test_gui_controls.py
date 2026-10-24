"""実GUIなしで、画面内の操作配置・明示操作・常駐準備の境界を検証する。"""
import re
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import app


class GuiControls(unittest.TestCase):
    def test_connection_does_not_request_exit_restart_or_consent_on_either_platform(self):
        messagebox = patch('app.messagebox', Mock())
        messagebox.start()
        self.addCleanup(messagebox.stop)
        for platform in ('win32', 'darwin'):
            for phase in ('confirmation_required', 'connecting', 'ready', None):
                with self.subTest(platform=platform, phase=phase):
                    gui = self.gui()
                    gui.db = Mock()
                    with patch('app.sys.platform', platform), patch(
                            'app.connection_options', return_value={'_connection_phase': phase}), patch(
                            'app.start_auto_connection') as start, patch(
                            'app.set_auto_watch') as enable, patch(
                            'app.messagebox.askyesno') as ask, patch(
                            'connection_settings.acknowledge_managed') as acknowledge, patch(
                            'app.prepare_queue_connection', side_effect=ValueError('unavailable')), patch(
                            'app.messagebox.showinfo') as notice:
                        self.assertFalse(gui.request_desktop_connection(enable_auto_resume=True))
                    self.assertIn('接続を確認できません', notice.call_args.args[1])
                    self.assertNotIn('起動アイコン', notice.call_args.args[1])
                    self.assertNotIn('開き直', notice.call_args.args[1])
                    start.assert_not_called()
                    ask.assert_not_called()
                    acknowledge.assert_not_called()
                    enable.assert_not_called()
                    gui.db.execute.assert_called_once_with('PRAGMA database_list')
                    gui.db.commit.assert_not_called()

    def gui(self):
        gui = app.GUI.__new__(app.GUI)
        gui.closing, gui.tray, gui.window_hidden = False, None, False
        gui.root = Mock()
        gui.root.winfo_screenwidth.return_value = 800
        gui.root.winfo_screenheight.return_value = 600
        gui.rows, gui.known = [], []
        for name in ('search', 'filter', 'details', 'status', 'tray_state', 'usage_status', 'auto_status', 'plan_note'):
            setattr(gui, name, Mock())
        gui.update_connection_note = Mock()
        gui.refresh_auto_status = Mock()
        return gui

    def build(self, gui):
        records = []
        def factory(kind):
            def create(*args, **kwargs):
                widget = Mock(name=kind)
                records.append((kind, widget, args, kwargs))
                return widget
            return Mock(side_effect=create)
        ttk = SimpleNamespace(**{name: factory(name) for name in
                              ('Frame', 'Label', 'Button', 'Entry', 'Combobox',
                               'Treeview', 'Scrollbar', 'Menubutton', 'Radiobutton', 'Checkbutton')})
        tk = SimpleNamespace(StringVar=Mock, BooleanVar=Mock, Text=factory('Text'), Menu=factory('Menu'))
        with patch('app.ttk', ttk), patch('app.tk', tk), patch('app.QuotaCards'):
            gui.build()
        return records

    def test_wrapping_uses_each_labels_allocated_width(self):
        gui = self.gui()
        self.build(gui)
        for label in gui.wrap_labels:
            label.reset_mock()
            gui.resize_label_text(SimpleNamespace(widget=label, width=900))
            self.assertEqual(label.configure.call_args.kwargs['wraplength'], 900)
            gui.resize_label_text(SimpleNamespace(widget=label, width=360))
            self.assertEqual(label.configure.call_args.kwargs['wraplength'], 360)
        self.assertNotIn('<Configure>', [call.args[0] for call in gui.root.bind.call_args_list])

    def test_settings_menu_names_explain_effect_without_automatic_execution(self):
        gui = self.gui()
        gui.reconnect_auto_watch, gui.setup_connection, gui.refresh_all = Mock(), Mock(), Mock()
        records = self.build(gui)
        menus = [widget for kind, widget, _, _ in records if kind == 'Menu']
        entries = {call.kwargs['label']: call.kwargs.get('command')
                   for call in menus[0].add_command.call_args_list}
        self.assertIn('一覧と利用枠を再取得（送信しない）', entries)
        self.assertIn('再開用の接続状態を確認…', entries)
        self.assertFalse(any('開き直' in label for label in entries))
        gui.reconnect_auto_watch.assert_not_called()
        gui.setup_connection.assert_not_called()
        entries['一覧と利用枠を再取得（送信しない）']()
        gui.refresh_all.assert_called_once()
        gui.reconnect_auto_watch.assert_not_called()

    def settings_menu(self, records):
        buttons = [widget for kind, widget, _, options in records
                   if kind == 'Menubutton' and options.get('text') == '設定']
        self.assertEqual(len(buttons), 1)
        menu = buttons[0].configure.call_args.kwargs['menu']
        self.assertIn(menu, [widget for kind, widget, _, _ in records if kind == 'Menu'])
        return menu

    def test_設定のチャット別操作は正しいハンドラと引数で実行する(self):
        gui = self.gui()
        actions = Mock()
        expected = {
            '実行・監視中を上に戻す': ('prioritize_active', ()),
            '選択したチャットだけON': ('toggle', (True,)),
            '完了として記録（監視を終了）': ('finish', ('completed',)),
            '要確認として記録（自動再開を停止）': ('finish', ('blocked',)),
            '確認して選び直す（試行履歴は保持）': ('review_reset', ()),
            '詳細情報（ID・場所）': ('show_technical_details', ()),
        }
        for name, _ in expected.values():
            setattr(gui, name, getattr(actions, name))
        menu = self.settings_menu(self.build(gui))
        self.assertEqual(actions.mock_calls, [], 'メニューの作成だけでは操作しません')
        commands = menu.add_command.call_args_list
        labels = [entry.kwargs['label'] for entry in commands]
        for label, (name, args) in expected.items():
            with self.subTest(操作=label):
                self.assertEqual(labels.count(label), 1)
                actions.reset_mock()
                commands[labels.index(label)].kwargs['command']()
                self.assertEqual(actions.mock_calls, [getattr(call, name)(*args)])
        section = commands[labels.index('チャット別の操作')]
        self.assertEqual(section.kwargs['state'], 'disabled')
        self.assertNotIn('command', section.kwargs)
        section_index = menu.mock_calls.index(call.add_command(label='チャット別の操作', state='disabled'))
        self.assertEqual(menu.mock_calls[section_index - 1], call.add_separator())
        last_index = next(i for i, entry in enumerate(menu.mock_calls)
                          if entry.kwargs.get('label') == '詳細情報（ID・場所）')
        self.assertEqual(menu.mock_calls[last_index + 1], call.add_separator())
        for label in expected:
            index = next(i for i, entry in enumerate(menu.mock_calls)
                         if entry.kwargs.get('label') == label)
            self.assertLess(section_index, index)
            self.assertLessEqual(index, last_index)

    def test_手順書が参照する設定の操作名は画面に実在する(self):
        gui = self.gui()
        menu = self.settings_menu(self.build(gui))
        labels = {entry.kwargs['label'] for entry in menu.add_command.call_args_list
                  if callable(entry.kwargs.get('command')) and entry.kwargs.get('state') != 'disabled'}
        root = Path(__file__).resolve().parents[1]
        paths = sorted(root.glob('*.md')) + sorted((root / 'docs').rglob('*.md'))
        paths += sorted((root / 'packaging').rglob('*.md'))
        references = {}
        for path in paths:
            operations = re.findall(r'「設定」(?:→|の)「([^」]+)」', path.read_text(encoding='utf-8'))
            references[path] = set(operations)
            for operation in operations:
                with self.subTest(文書=str(path.relative_to(root)), 操作=operation):
                    self.assertIn(operation, labels, '手順書の操作名を設定メニューと合わせてください')
        acceptance = references[root / 'docs' / '実機受入手順.md']
        self.assertTrue({
            '実行・監視中を上に戻す', '選択したチャットだけON',
            '完了として記録（監視を終了）', '要確認として記録（自動再開を停止）',
            '確認して選び直す（試行履歴は保持）', '詳細情報（ID・場所）',
        } <= acceptance, '受入手順には復旧した６操作を記載してください')

    def test_行が未選択のメニュー操作は日本語で案内して状態を変えない(self):
        gui = self.gui()
        gui.db, gui.refresh, gui.write = Mock(), Mock(), Mock()
        menu = self.settings_menu(self.build(gui))
        gui.selected = Mock(return_value=None)
        entries = {entry.kwargs['label']: entry.kwargs.get('command')
                   for entry in menu.add_command.call_args_list}
        with patch('app.messagebox') as dialogs, patch('app.enable') as enable, patch('app.mark') as mark:
            for label in ('選択したチャットだけON', '完了として記録（監視を終了）',
                          '要確認として記録（自動再開を停止）',
                          '確認して選び直す（試行履歴は保持）', '詳細情報（ID・場所）'):
                with self.subTest(操作=label):
                    dialogs.reset_mock()
                    entries[label]()
                    dialogs.showinfo.assert_called_once_with(
                        'チャットを選択してください', '一覧から操作するチャットの行を選んでください。', parent=gui.root)
                    dialogs.askyesno.assert_not_called()
            enable.assert_not_called()
            mark.assert_not_called()
        gui.db.execute.assert_not_called()
        gui.refresh.assert_not_called()
        gui.write.assert_not_called()

    def test_メニューの個別操作も全体停止と他の行と試行履歴を保持する(self):
        import auto_watch
        import core
        import watch
        from tests.test_resume_list import ResumeList

        fixture = ResumeList()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        tid = fixture.add(1, '操作するチャット', limited=True)
        other = fixture.add(2, '別のチャット', limited=True)
        auto_watch.initialize(fixture.db)
        core.enable(fixture.db, tid, False)
        core.enable(fixture.db, other, True)
        fixture.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                           (tid, '過去の停止', '過去の試行', 1, 'received', '受領ターン'))
        attempts = [tuple(row) for row in fixture.db.execute('SELECT * FROM attempts')]
        policy = auto_watch.status(fixture.db)
        gui = self.gui()
        gui.db = fixture.db
        gui.refresh, gui.write = Mock(), Mock()
        menu = self.settings_menu(self.build(gui))
        gui.selected = Mock(return_value=tid)
        entries = {entry.kwargs['label']: entry.kwargs.get('command')
                   for entry in menu.add_command.call_args_list}
        other_before = next(row for row in core.list_threads(gui.db) if row['id'] == other)
        entries['選択したチャットだけON']()
        self.assertTrue(next(row for row in core.list_threads(gui.db) if row['id'] == tid)['enabled'])
        for label, state in (('完了として記録（監視を終了）', 'completed'),
                             ('要確認として記録（自動再開を停止）', 'blocked')):
            with self.subTest(操作=label):
                before = core.list_threads(gui.db)
                fixture.dialogs.askyesno.return_value = False
                entries[label]()
                self.assertEqual(core.list_threads(gui.db), before)
                self.assertEqual(fixture.dialogs.askyesno.call_args.kwargs['default'], 'no')
                fixture.dialogs.askyesno.return_value = True
                entries[label]()
                row = next(row for row in core.list_threads(gui.db) if row['id'] == tid)
                self.assertEqual(row['state'], state)
                self.assertFalse(row['enabled'])
                entries['選択したチャットだけON']()
                self.assertEqual(next(row for row in core.list_threads(gui.db) if row['id'] == tid), row)
        self.assertEqual(auto_watch.status(gui.db), policy)
        self.assertFalse(policy['enabled'])
        self.assertEqual(next(row for row in core.list_threads(gui.db) if row['id'] == other), other_before)
        self.assertEqual([tuple(row) for row in gui.db.execute('SELECT * FROM attempts')], attempts)
        with patch('watch.discover') as discover:
            result = watch.once(gui.db, lambda: self.fail('全体停止中は接続しません'))
        self.assertEqual(result['state'], 'disabled')
        discover.assert_not_called()

    def test_unselected_details_view_does_not_point_to_removed_batch_action(self):
        gui = self.gui()
        gui.simple_mode = False
        gui.selected = Mock(return_value=None)
        gui.select_row()
        text = gui.details.set.call_args.args[0]
        self.assertNotIn('まとめてON', text)
        self.assertIn('対象チェック', text)

    def test_initial_geometry_fits_small_screens_without_maximizing(self):
        for screen in ((800, 600), (1024, 768), (1280, 800)):
            with self.subTest(screen=screen):
                gui = self.gui()
                gui.root.winfo_screenwidth.return_value, gui.root.winfo_screenheight.return_value = screen
                gui.configure_window()
                width, height, x, y = map(int, re.fullmatch(r'(\d+)x(\d+)\+(\d+)\+(\d+)',
                                         gui.root.geometry.call_args.args[0]).groups())
                self.assertLessEqual(width + x, screen[0])
                self.assertLessEqual(height + y, screen[1])
                self.assertLess(width, screen[0])
                self.assertLess(height, screen[1])
                self.assertLessEqual(gui.root.minsize.call_args.args[0], width)
                self.assertLessEqual(gui.root.minsize.call_args.args[1], height)
                gui.root.state.assert_not_called()

    def test_controls_use_explicit_actions_and_both_scroll_directions(self):
        gui = self.gui()
        gui.toggle_auto_watch = Mock()
        records = self.build(gui)
        buttons = {options.get('text'): (widget, options) for kind, widget, _, options in records if kind == 'Button'}
        self.assertEqual(set(buttons), {'自動再開をON', '接続の確認・修復…', '一覧・利用枠を更新'})
        buttons['自動再開をON'][1]['command']()
        gui.toggle_auto_watch.assert_called_once()
        orientations = {options['orient'] for kind, _, _, options in records if kind == 'Scrollbar'}
        self.assertEqual(orientations, {'horizontal', 'vertical'})
        self.assertNotIn('<Double-1>', [call.args[0] for call in gui.tree.bind.call_args_list])
        weighted_rows = [call for call in gui.root.rowconfigure.call_args_list if call.kwargs.get('weight') == 1]
        self.assertEqual(len(weighted_rows), 1)


    def test_selection_summary_omits_ids_and_absolute_paths(self):
        gui = self.gui()
        gui.selected = lambda: 'private-id'
        gui.rows = [dict(id='private-id', title='タイトル', cwd='/private/parent/project')]
        gui.select_row()
        text = gui.details.set.call_args.args[0]
        self.assertNotIn('private-id', text)
        self.assertNotIn('/private/parent', text)
        self.assertIn('タイトル', text)

    def test_dock_reopen_restores_existing_window(self):
        gui = self.gui()
        with patch('app.sys.platform', 'darwin'):
            gui.configure_reopen()
        gui.root.createcommand.assert_called_once_with('tk::mac::ReopenApplication', gui.show_window)
        gui.root.createcommand.call_args.args[1]()
        gui.root.deiconify.assert_called_once()
        gui.root.lift.assert_called_once()


class FakeIcon:
    def __init__(self, fail=False):
        self._visible = False
        self.fail = fail
        self.ready = False
        self.setter_threads = []
        self.setup = None
        self.stopped = False
        self.run_called = threading.Event()
        self.exit_run = threading.Event()
        self.run_thread = None
        self.menu_updates = 0

    def update_menu(self):
        self.menu_updates += 1

    def run_detached(self, setup=None):
        self.setup = setup
        if setup is not None:
            self.ready = True
            setup(self)

    def run(self):
        self.run_thread = threading.current_thread()
        self.run_called.set()
        self.exit_run.wait()

    @property
    def visible(self):
        return self._visible

    @visible.setter
    def visible(self, value):
        if not self.ready or self.fail:
            raise RuntimeError('fixture not ready')
        self.setter_threads.append(threading.get_ident())
        self._visible = value

    def stop(self):
        self.stopped = True
        self.exit_run.set()


class TrayControls(unittest.TestCase):
    def gui(self):
        gui = app.GUI.__new__(app.GUI)
        gui.root, gui.tray_state, gui.write = Mock(), Mock(), Mock()
        gui.tray, gui.tray_available = None, False
        gui.tray_requested, gui.closing = True, False
        return gui

    def start(self, gui, icon, platform):
        def stop_fixture():
            icon.stop()
            thread = getattr(gui, 'tray_thread', None)
            if thread is not None:
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.addCleanup(stop_fixture)
        pystray = SimpleNamespace(Menu=Mock(), MenuItem=Mock(), Icon=Mock(return_value=icon))
        with patch('app.sys.platform', platform), patch('app.tray_dependencies', return_value=(pystray, Mock(), Mock())), \
                patch('app.create_tray_image'):
            gui.start_tray()
        return pystray

    def test_mac_visibility_is_set_on_calling_tk_thread_after_detach(self):
        gui, icon = self.gui(), FakeIcon()
        self.start(gui, icon, 'darwin')
        self.assertIsNotNone(icon.setup)
        self.assertEqual(icon.setter_threads, [threading.get_ident()])
        self.assertTrue(gui.tray_available)
        self.assertIn('メニューバー', gui.tray_state.set.call_args.args[0])

    def test_windows_waits_for_backend_visibility_without_setting_it_early(self):
        gui, icon = self.gui(), FakeIcon()
        self.start(gui, icon, 'win32')
        self.assertIsNone(icon.setup)
        self.assertEqual(icon.setter_threads, [])
        self.assertFalse(gui.tray_available)
        icon._visible = True  # Windows backend reports readiness later.
        with patch('app.sys.platform', 'win32'):
            gui.check_tray_ready()
        self.assertTrue(gui.tray_available)
        self.assertIn('タスクトレイ', gui.tray_state.set.call_args.args[0])

    def test_windows_public_run_and_its_setup_thread_are_daemons(self):
        release = threading.Event()
        class FailingBackend(FakeIcon):
            def run(self):
                self.run_thread = threading.current_thread()
                # Model pystray's default setup waiting for native readiness.
                self.setup_thread = threading.Thread(target=release.wait)
                self.setup_thread.start()
                self.run_called.set()
                raise RuntimeError('private backend startup failure')
        gui, icon = self.gui(), FailingBackend()
        try:
            with patch('threading.excepthook') as thread_error:
                self.start(gui, icon, 'win32')
                self.assertTrue(icon.run_called.wait(1), 'public Icon.run was not called')
                self.assertIs(gui.tray_thread, icon.run_thread)
                self.assertTrue(gui.tray_thread.daemon)
                self.assertTrue(icon.setup_thread.daemon)
                gui.tray_thread.join(1)
                self.assertFalse(gui.tray_thread.is_alive())
                thread_error.assert_not_called()
            self.assertFalse(gui.tray_available)
            gui.check_tray_ready(remaining=0)
            gui.root.deiconify.assert_called()
        finally:
            release.set()
            if hasattr(icon, 'setup_thread'):
                icon.setup_thread.join(1)
                self.assertFalse(icon.setup_thread.is_alive())

    def ready_windows_icon(self):
        gui, icon = self.gui(), FakeIcon()
        self.start(gui, icon, 'win32')
        self.assertTrue(icon.run_called.wait(1))
        icon._visible = True
        gui.check_tray_ready()
        self.assertTrue(gui.tray_available)
        return gui, icon

    def test_backend_exit_after_ready_restores_window_on_tk_health_callback(self):
        gui, icon = self.ready_windows_icon()
        health_callback = gui.root.after.call_args.args[1]
        gui.on_close()
        gui.root.withdraw.assert_called_once()
        before = list(gui.root.mock_calls)
        icon.exit_run.set()
        gui.tray_thread.join(1)
        self.assertFalse(gui.tray_thread.is_alive())
        self.assertEqual(gui.root.mock_calls, before, 'backend thread must not call Tk')
        health_callback()
        self.assertFalse(gui.tray_available)
        gui.root.deiconify.assert_called_once()
        gui.on_close()
        gui.root.withdraw.assert_called_once()

    def test_close_rejects_cached_visible_after_backend_exit_before_health_poll(self):
        gui, icon = self.ready_windows_icon()
        icon.exit_run.set()
        gui.tray_thread.join(1)
        self.assertTrue(icon.visible, 'fixture preserves pystray cached visibility')
        gui.on_close()
        gui.root.withdraw.assert_not_called()
        gui.root.deiconify.assert_called_once()
        self.assertFalse(gui.tray_available)

    def test_expected_quit_does_not_restore_or_reschedule_after_backend_exit(self):
        gui, icon = self.ready_windows_icon()
        gui.db = Mock()
        callback = gui.root.after.call_args.args[1]
        with patch('app.set_auto_watch'), patch('queue_state.pending', return_value=[]):
            gui.quit_app()
        gui.tray_thread.join(1)
        self.assertTrue(gui.closing)
        before = list(gui.root.mock_calls)
        callback()
        gui.on_close()
        self.assertEqual(gui.root.mock_calls, before)
        gui.root.deiconify.assert_not_called()
        gui.root.destroy.assert_called_once()

    def test_tray_menu_enqueues_without_entering_tcl_from_native_callback(self):
        gui, icon = self.gui(), FakeIcon()
        pystray = self.start(gui, icon, 'darwin')
        gui.root.state.return_value = 'withdrawn'
        gui.root.reset_mock()
        pystray.MenuItem.call_args_list[0].args[1](None, None)
        self.assertEqual(gui.root.mock_calls, [])
        gui.drain_tray_actions()
        gui.root.deiconify.assert_called_once()
        gui.root.lift.assert_called_once()

    def test_worker_tray_callback_does_not_touch_tk(self):
        gui, icon = self.gui(), FakeIcon()
        pystray = self.start(gui, icon, 'win32')
        gui.root.state.return_value = 'withdrawn'
        gui.root.reset_mock()
        worker = threading.Thread(target=pystray.MenuItem.call_args_list[0].args[1],
                                  args=(None, None))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(gui.root.mock_calls, [])
        gui.drain_tray_actions()
        gui.root.deiconify.assert_called_once()

    def test_tray_label_and_action_follow_hide_restore_and_minimize(self):
        gui, icon = self.gui(), FakeIcon()
        gui.root.state.return_value = 'normal'
        gui.root.withdraw.side_effect = lambda: setattr(gui.root.state, 'return_value', 'withdrawn')
        gui.root.deiconify.side_effect = lambda: setattr(gui.root.state, 'return_value', 'normal')
        pystray = self.start(gui, icon, 'darwin')
        label, action = pystray.MenuItem.call_args_list[0].args[:2]
        self.assertEqual(label(None), 'トレイにしまう')
        gui.root.reset_mock()
        action(None, None)
        self.assertEqual(gui.root.mock_calls, [])
        gui.drain_tray_actions()
        gui.root.withdraw.assert_called_once()
        self.assertEqual(label(None), 'ウィンドウを開く')
        self.assertEqual(icon.menu_updates, 1)
        action(None, None)
        gui.drain_tray_actions()
        gui.root.deiconify.assert_called_once()
        self.assertEqual(label(None), 'トレイにしまう')
        self.assertEqual(icon.menu_updates, 2)
        gui.root.state.return_value = 'iconic'
        gui.sync_tray_window(SimpleNamespace(widget=gui.root))
        self.assertEqual(label(None), 'ウィンドウを開く')

    def test_native_label_reader_never_calls_tk(self):
        gui, icon = self.gui(), FakeIcon()
        gui.root.state.return_value = 'normal'
        pystray = self.start(gui, icon, 'darwin')
        label = pystray.MenuItem.call_args_list[0].args[0]
        gui.root.reset_mock()
        values = []
        worker = threading.Thread(target=lambda: values.append(label(None)))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values, ['トレイにしまう'])
        self.assertEqual(gui.root.mock_calls, [])

    def test_child_unmap_does_not_change_tray_label(self):
        gui, icon = self.gui(), FakeIcon()
        pystray = self.start(gui, icon, 'darwin')
        label = pystray.MenuItem.call_args_list[0].args[0]
        gui.root.reset_mock()
        gui.sync_tray_window(SimpleNamespace(widget=object()))
        self.assertEqual(gui.root.mock_calls, [])
        self.assertEqual(label(None), 'トレイにしまう')
        self.assertEqual(icon.menu_updates, 0)

    def test_queued_tray_actions_are_discarded_after_close(self):
        gui, icon = self.gui(), FakeIcon()
        pystray = self.start(gui, icon, 'darwin')
        pystray.MenuItem.call_args_list[0].args[1](None, None)
        gui.closing = True
        gui.root.reset_mock()
        gui.drain_tray_actions()
        self.assertEqual(gui.root.mock_calls, [])

    def test_slow_backend_retains_handle_until_ready_and_remains_controllable(self):
        gui, icon = self.gui(), FakeIcon()
        gui.db = Mock()
        self.start(gui, icon, 'win32')
        self.assertTrue(icon.run_called.wait(1))
        with patch.object(icon, 'stop', wraps=icon.stop) as stop:
            gui.check_tray_ready(remaining=0)
            self.assertIs(gui.tray, icon)
            self.assertFalse(gui.tray_available)
            stop.assert_not_called()
            gui.root.deiconify.assert_called_once()
            gui.root.withdraw.assert_not_called()
            self.assertEqual(gui.root.after.call_args.args[0], 1000)
            self.assertIn('待', gui.tray_state.set.call_args.args[0])
            gui.root.after.call_args.args[1]()
            self.assertIs(gui.tray, icon)
            stop.assert_not_called()
            gui.root.deiconify.assert_called_once()  # Waiting must not steal focus every second.
            self.assertEqual(gui.root.after.call_args.args[0], 1000)
            icon._visible = True  # Native readiness arrives after the initial observation window.
            gui.root.after.call_args.args[1]()
            self.assertIs(gui.tray, icon)
            self.assertTrue(gui.tray_available)
            gui.on_close()
            gui.root.withdraw.assert_called_once()
            with patch('app.set_auto_watch'), patch('queue_state.pending', return_value=[]):
                gui.quit_app()
            stop.assert_called_once()
            gui.tray_thread.join(1)
            self.assertFalse(gui.tray_thread.is_alive())
            gui.root.destroy.assert_called_once()

    def test_show_failure_keeps_window_visible_and_unavailable(self):
        gui, icon = self.gui(), FakeIcon(fail=True)
        self.start(gui, icon, 'darwin')
        self.assertFalse(gui.tray_available)
        gui.root.withdraw.assert_not_called()
        gui.root.deiconify.assert_called()
        self.assertTrue(icon.stopped)

    def test_close_hides_only_when_icon_is_still_visible(self):
        gui, icon = self.gui(), FakeIcon()
        gui.tray, gui.tray_available = icon, True
        gui.quit_app = Mock()
        gui.on_close()
        gui.root.withdraw.assert_not_called()
        gui.quit_app.assert_not_called()
        icon._visible = True
        gui.tray_available = True
        gui.on_close()
        gui.root.withdraw.assert_called_once()
        gui.tray_requested, gui.tray_available = False, False
        gui.on_close()
        gui.quit_app.assert_called_once()
