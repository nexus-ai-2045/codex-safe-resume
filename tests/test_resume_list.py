"""再開予定の表示、並び替え、一括選択の送信しない境界。"""
from contextlib import closing
from pathlib import Path
import tempfile
import re
import unittest
from unittest.mock import Mock, patch

import app
import core


class ResumeList(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = core.connection(Path(self.temp.name) / 'state')
        self.addCleanup(self.db.close)
        self.gui = app.GUI.__new__(app.GUI)
        self.gui.db = self.db
        self.gui.root = Mock()
        self.gui.tree = Mock()
        self.gui.tree.get_children.return_value = []
        self.gui.selected = Mock(return_value=None)
        self.gui.select_row = Mock()
        self.gui.update_status = Mock()
        self.gui.write = Mock()
        self.gui.search = Mock(); self.gui.search.get.return_value = ''
        self.gui.filter = Mock(); self.gui.filter.get.return_value = 'すべて'
        self.gui.known, self.gui.rows = [], []
        self.gui.confirm_limited_targets = Mock(side_effect=lambda rows: [row['id'] for row in rows])
        discovery = patch('app.discover', side_effect=lambda **kwargs: (self.gui.known, None))
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)
        dialogs = patch('app.messagebox')
        self.dialogs = dialogs.start()
        self.addCleanup(dialogs.stop)

    def add(self, number, title, *, limited=False, enabled=False, state='idle', turn_status='failed'):
        tid = f'123e4567-e89b-12d3-a456-426614174{number:03d}'
        observed = dict(id=tid, title=title, cwd='', limited=limited,
                        failure_turn='failure' if limited else None, turn_status=turn_status)
        core.sync(self.db, [observed])
        self.db.execute('UPDATE threads SET enabled=?, state=? WHERE id=?', (enabled, state, tid))
        self.gui.known.append(observed)
        self.gui.rows = core.list_threads(self.db)
        return tid

    def order(self):
        return [call.kwargs['iid'] for call in self.gui.tree.insert.call_args_list]

    def test_active_first_default_and_manual_sort_orders_entire_list(self):
        idle = self.add(1, 'A')
        waiting = self.add(2, 'Z', limited=True, enabled=True, state='waiting')
        running = self.add(3, 'B', turn_status='inProgress')
        self.gui.render_rows()
        self.assertEqual(self.order(), [running, waiting, idle])
        self.gui.tree.insert.reset_mock()
        self.gui.sort_rows('title')
        self.assertEqual(self.order(), [idle, running, waiting])
        self.gui.tree.insert.reset_mock()
        self.gui.sort_rows('title')
        self.assertEqual(self.order(), [waiting, running, idle])
        self.gui.tree.insert.reset_mock()
        self.gui.render_rows()
        self.assertEqual(self.order(), [waiting, running, idle])
        self.gui.tree.insert.reset_mock()
        self.gui.prioritize_active()
        self.assertEqual(self.order(), [running, waiting, idle])

    def test_circled_number_titles_are_text_and_do_not_break_rendering(self):
        a = self.add(1, '①', limited=True)
        b = self.add(2, '②', limited=True)
        self.gui.render_rows()
        self.assertEqual(set(self.order()), {a, b})
        self.gui.tree.insert.reset_mock()
        self.gui.sort_rows('title')
        self.assertEqual(self.order(), [a, b])

    def test_auto_column_sort_is_reflected_in_sort_control(self):
        off = self.add(1, 'OFF', limited=True)
        on = self.add(2, 'ON', enabled=True, state='waiting')
        self.gui.sort_choice = Mock()
        core.enable(self.db, off, False)
        self.gui.rows = core.list_threads(self.db)
        self.gui.sort_rows('auto')
        self.assertEqual(self.order(), [off, on])
        self.gui.sort_choice.set.assert_called_with('チャット別の自動再開：昇順')
        self.gui.tree.insert.reset_mock()
        self.gui.sort_rows('auto')
        self.assertEqual(self.order(), [on, off])
        self.gui.sort_choice.set.assert_called_with('チャット別の自動再開：降順')

    def test_explicit_sort_control_reorders_all_rows_and_survives_refresh(self):
        a = self.add(1, 'タスク2', limited=True)
        b = self.add(2, 'タスク10', limited=True)
        self.gui.simple_mode = True
        self.gui.sort_choice = Mock()
        self.gui.sort_choice.get.return_value = 'チャット名：昇順'
        self.gui.apply_sort_choice()
        self.assertEqual(self.order(), [a, b])
        self.gui.tree.insert.reset_mock()
        self.gui.sort_choice.get.return_value = 'チャット名：降順'
        self.gui.apply_sort_choice()
        self.assertEqual(self.order(), [b, a])
        self.gui.tree.insert.reset_mock()
        self.gui.refresh()
        self.assertEqual(self.order(), [b, a])
        self.gui.tree.insert.reset_mock()
        self.gui.sort_rows('title')
        self.assertEqual(self.order(), [a, b])
        self.gui.sort_choice.set.assert_called_with('チャット名：昇順')

    def test_excluded_and_stale_cached_rows_are_not_visible(self):
        visible = self.add(1, '表示する')
        archived = self.add(2, '除外済み')
        child = self.add(3, '内部処理')
        stale = self.add(4, '古いキャッシュ')
        self.gui.known = [dict(self.gui.known[0]),
                          dict(self.gui.known[1], excluded_reason='archived'),
                          dict(self.gui.known[2], excluded_reason='subagent')]
        self.gui.render_rows()
        self.assertEqual(self.order(), [visible])

    def test_date_sort_uses_numeric_values_and_missing_remains_last(self):
        a = self.add(1, 'A', enabled=True)
        b = self.add(2, 'B', enabled=True)
        c = self.add(3, 'C', enabled=True)
        self.db.execute('UPDATE threads SET reset_at=1900000000 WHERE id=?', (a,))
        self.db.execute('UPDATE threads SET reset_at=1800000000 WHERE id=?', (b,))
        self.gui.rows = core.list_threads(self.db)
        self.gui.sort_rows('reset')
        self.assertEqual(self.order(), [b, a, c])
        self.gui.tree.insert.reset_mock()
        self.gui.sort_rows('reset')
        self.assertEqual(self.order(), [a, b, c])

    def test_missing_date_explains_connection_and_monitor_state(self):
        tid = self.add(1, '候補', limited=True)
        self.gui.render_rows()
        self.assertIn('接続', self.gui.tree.insert.call_args.kwargs['values'][4])
        self.gui.tree.insert.reset_mock()
        with patch('app.connection_enabled', return_value=True):
            self.gui.render_rows()
        self.assertIn('OFF', self.gui.tree.insert.call_args.kwargs['values'][4])
        core.enable(self.db, tid, True)
        self.gui.rows = core.list_threads(self.db)
        with patch('app.connection_enabled', return_value=True):
            self.gui.render_rows()
        self.assertIn('取得待ち', self.gui.tree.insert.call_args.kwargs['values'][4])

    def test_quota_wait_updates_rows_without_send_results(self):
        self.gui.closing = False
        tid = self.add(1, '対象', limited=True, enabled=True, state='waiting')
        self.db.execute('UPDATE threads SET reset_at=1900000000 WHERE id=?', (tid,))
        self.gui.render_rows = Mock()
        self.gui.monitor_result({'state': 'quota-wait', 'armed': 1})
        self.gui.render_rows.assert_called_once()
        self.assertEqual(self.gui.rows[0]['reset_at'], 1900000000)
        self.assertFalse(self.gui.monitor_running)

    def test_batch_only_enables_confirmed_quota_candidates_and_never_sends(self):
        a = self.add(1, '対象', limited=True)
        self.add(2, '正常')
        self.add(3, '要確認', limited=True, state='blocked')
        self.add(4, '完了', limited=True, state='completed')
        self.gui.refresh = Mock()
        with patch('app.discover', return_value=(self.gui.known, None)), patch('app.messagebox'), patch('app.monitor_once') as sender:
            self.gui.enable_limited()
            sender.assert_not_called()
            self.assertEqual([row['id'] for row in self.gui.confirm_limited_targets.call_args.args[0]], [a])
        enabled = [row['id'] for row in core.list_threads(self.db) if row['enabled']]
        self.assertEqual(enabled, [a])
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_monitor_render_does_not_rescan_or_write_discovery(self):
        self.gui.closing = False
        self.gui.render_rows = Mock()
        with patch('app.discover') as discovery, patch('app.sync') as sync:
            self.gui.monitor_result({'state': 'offline'})
        discovery.assert_not_called()
        sync.assert_not_called()
        self.gui.render_rows.assert_called_once()

    def test_batch_all_candidates_are_offered_despite_main_filter_and_only_subset_enabled(self):
        ids = [self.add(n, '同名', limited=True) for n in range(1, 12)]
        self.db.executemany('UPDATE threads SET updated_at=? WHERE id=?',
                            [(1_700_000_000 + index, tid) for index, tid in enumerate(ids)])
        self.gui.rows = core.list_threads(self.db)
        self.gui.search.get.return_value = '見つからない'
        self.gui.filter.get.return_value = '完了'
        self.gui.refresh = Mock()
        self.gui.confirm_limited_targets.side_effect = None
        self.gui.confirm_limited_targets.return_value = [ids[2], ids[10]]
        with patch('app.discover', return_value=(self.gui.known, None)) as discovery, patch('app.monitor_once') as sender:
            self.gui.enable_limited()
        offered = self.gui.confirm_limited_targets.call_args.args[0]
        self.assertEqual([row['id'] for row in offered], list(reversed(ids)))
        discovery.assert_called_once_with(include_ids=[ids[10], ids[2]])
        self.assertEqual([row['id'] for row in core.list_threads(self.db) if row['enabled']],
                         [ids[10], ids[2]])
        sender.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_batch_invalid_or_duplicate_selection_fails_closed(self):
        candidate = self.add(1, '候補', limited=True)
        other = self.add(2, '正常')
        self.gui.refresh = Mock()
        self.gui.confirm_limited_targets.side_effect = None
        for selection in ([other], [candidate, candidate], [candidate, other], candidate, [None]):
            with self.subTest(selection=selection), patch('app.discover') as discovery, patch('app.messagebox') as dialogs:
                self.gui.confirm_limited_targets.return_value = selection
                self.gui.enable_limited()
                discovery.assert_not_called()
                dialogs.showwarning.assert_called_once()
                self.assertFalse(any(row['enabled'] for row in core.list_threads(self.db)))

    def test_target_dialog_shows_all_full_titles_project_and_id_and_accepts_subset(self):
        title = '長い同名のタイトル' * 30
        ids = [self.add(n, title, limited=True) for n in range(1, 12)]
        self.db.executemany('UPDATE threads SET updated_at=? WHERE id=?',
                            [(1_700_000_000 + index, tid) for index, tid in enumerate(ids)])
        self.db.execute('UPDATE threads SET cwd=? WHERE id=?', ('/private/project-a', ids[0]))
        self.db.execute('UPDATE threads SET cwd=? WHERE id=?', ('/private/project-b', ids[1]))
        rows = core.list_threads(self.db)
        # Distinct timestamps force a different order from insertion, without
        # relying on whether the fixture crosses a wall-clock second boundary.
        self.assertEqual([row['id'] for row in rows], list(reversed(ids)))
        with patch('app.tk') as tk, patch('app.ttk') as ttk, patch('app.tkfont') as fonts:
            fonts.nametofont.return_value.measure.side_effect = len
            dialog = tk.Toplevel.return_value
            dialog.winfo_screenwidth.return_value = 800
            dialog.winfo_screenheight.return_value = 600
            tree = ttk.Treeview.return_value
            tree.selection.return_value = (ids[0], ids[10])
            def confirm():
                commands = {call.kwargs['text']: call.kwargs.get('command') for call in ttk.Button.call_args_list}
                commands['選択した対象をON']()
            dialog.wait_window.side_effect = confirm
            result = app.GUI.confirm_limited_targets(self.gui, rows)
            self.assertEqual(result, [ids[0], ids[10]])
            self.assertEqual(len(tree.insert.call_args_list), 11)
            values = [call.kwargs['values'] for call in tree.insert.call_args_list]
            projects = {ids[0]: 'project-a', ids[1]: 'project-b'}
            self.assertEqual(values, [(title, projects.get(row['id'], '—'), row['id'])
                                      for row in rows])
            self.assertEqual([call.kwargs['iid'] for call in tree.insert.call_args_list],
                             [row['id'] for row in rows])
            self.assertEqual(ttk.Treeview.call_args.kwargs['selectmode'], 'extended')
            tree.selection_set.assert_not_called()
            dialog.grab_set.assert_called_once()
            dialog.wait_window.assert_called_once()
            self.assertEqual(tk.StringVar.call_args.kwargs['value'], '選択 0件 / 全11件')
            dialog.resizable.assert_called_once_with(True, True)
            orientations = {call.kwargs.get('orient') for call in ttk.Scrollbar.call_args_list}
            self.assertEqual(orientations, {'horizontal', 'vertical'})
            text = ' '.join(call.kwargs.get('text', '') for call in ttk.Label.call_args_list)
            self.assertIn('検索', text)
            self.assertIn('全', text)
            self.assertIn('今は送信', text)
            self.assertIn('OFF', text)

    def test_target_dialog_all_select_clear_cancel_and_window_close(self):
        ids = [self.add(n, '候補', limited=True) for n in (1, 2)]
        rows = core.list_threads(self.db)
        for cancel_route in ('取消し', 'Escape', 'window'):
            with self.subTest(route=cancel_route), patch('app.tk') as tk, patch('app.ttk') as ttk, patch('app.tkfont') as fonts:
                fonts.nametofont.return_value.measure.side_effect = len
                dialog = tk.Toplevel.return_value
                dialog.winfo_screenwidth.return_value = 800
                dialog.winfo_screenheight.return_value = 600
                tree = ttk.Treeview.return_value
                tree.get_children.return_value = tuple(ids)
                tree.selection.side_effect = [tuple(ids), tuple(ids), ()]
                def cancel():
                    commands = {call.kwargs['text']: call.kwargs.get('command') for call in ttk.Button.call_args_list}
                    commands['全件選択']()
                    tree.selection_set.assert_called_once_with(tuple(ids))
                    commands['全解除']()
                    tree.selection_remove.assert_called_once_with(tuple(ids))
                    self.assertEqual([call.args[0] for call in tk.StringVar.return_value.set.call_args_list],
                                     ['選択 2件 / 全2件', '選択 0件 / 全2件'])
                    if cancel_route == 'Escape':
                        dict(call.args for call in dialog.bind.call_args_list)['<Escape>'](Mock())
                    elif cancel_route == 'window':
                        dialog.protocol.call_args.args[1]()
                    else:
                        commands[cancel_route]()
                dialog.wait_window.side_effect = cancel
                self.assertEqual(app.GUI.confirm_limited_targets(self.gui, rows), [])

    def test_target_dialog_fits_small_screens_and_explains_multiple_selection(self):
        self.add(1, '候補', limited=True)
        rows = core.list_threads(self.db)
        for platform, key in (('darwin', '⌘'), ('win32', 'Ctrl')):
            for screen in ((800, 600), (640, 480)):
                with self.subTest(platform=platform, screen=screen), \
                        patch('app.sys.platform', platform), patch('app.tk') as tk, \
                        patch('app.ttk') as ttk, patch('app.tkfont') as fonts:
                    fonts.nametofont.return_value.measure.side_effect = len
                    dialog = tk.Toplevel.return_value
                    dialog.winfo_screenwidth.return_value, dialog.winfo_screenheight.return_value = screen
                    self.assertEqual(app.GUI.confirm_limited_targets(self.gui, rows), [])
                    geometry = dialog.geometry.call_args.args[0]
                    match = re.fullmatch(r'(\d+)x(\d+)\+(\d+)\+(\d+)', geometry)
                    self.assertIsNotNone(match)
                    width, height, x, y = map(int, match.groups())
                    self.assertLessEqual(width + x, screen[0])
                    self.assertLessEqual(height + y, screen[1])
                    self.assertLessEqual(dialog.minsize.call_args.args[0], width)
                    self.assertLessEqual(dialog.minsize.call_args.args[1], height)
                    text = ' '.join(call.kwargs.get('text', '') for call in ttk.Label.call_args_list)
                    self.assertIn(key, text)
                    self.assertIn('Shift', text)

    def test_batch_rechecks_saved_candidate_outside_recent_limit(self):
        import sqlite3
        tid = self.add(1, '以前の停止候補', limited=True)
        home = Path(self.temp.name) / 'codex'
        home.mkdir()
        with closing(sqlite3.connect(home / 'state_5.sqlite')) as source, source:
            source.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT)')
            source.execute('INSERT INTO threads VALUES (?,?,?)', (tid, '以前の停止候補', ''))
            source.executemany('INSERT INTO threads VALUES (?,?,?)',
                [(f'123e4567-e89b-12d3-a456-426614174{n:03d}', '新しい会話', '') for n in range(2, 403)])
        with closing(sqlite3.connect(home / 'thread_history_1.sqlite')) as history, history:
            history.execute('CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,error_json TEXT)')
            history.execute('INSERT INTO thread_turns VALUES (?,?,?,?)', (tid, 'failure', 'failed', '{"type":"usageLimitExceeded"}'))
        self.gui.refresh = Mock()
        with patch('app.discover', side_effect=lambda **kw: core.discover(home, **kw)), patch('app.messagebox') as dialogs:
            self.gui.enable_limited()
            dialogs.showwarning.assert_not_called()
        self.assertTrue(core.list_threads(self.db)[0]['enabled'])

    def test_batch_cancel_does_not_enable(self):
        self.add(1, '候補', limited=True)
        self.gui.refresh = Mock()
        with patch('app.messagebox'):
            self.gui.confirm_limited_targets.return_value = []
            self.gui.confirm_limited_targets.side_effect = None
            self.gui.enable_limited()
        self.discovery.assert_not_called()
        self.assertFalse(any(r['enabled'] for r in core.list_threads(self.db)))

    def test_batch_changed_generation_rolls_back_all(self):
        a = self.add(1, 'A', limited=True)
        b = self.add(2, 'B', limited=True)
        self.gui.refresh = Mock()
        def confirm(*args, **kwargs):
            self.db.execute('UPDATE threads SET generation=generation+1 WHERE id=?', (b,))
            return [a, b]
        self.gui.confirm_limited_targets.side_effect = confirm
        with patch('app.discover', return_value=(self.gui.known, None)), patch('app.messagebox') as dialogs:
            self.gui.enable_limited()
            dialogs.showwarning.assert_called_once()
        self.assertFalse(any(r['enabled'] for r in core.list_threads(self.db)))

    def test_batch_changed_failure_after_confirmation_does_not_enable(self):
        self.add(1, '候補', limited=True)
        self.gui.refresh = Mock()
        changed = [dict(self.gui.known[0], failure_turn='manual-change')]
        with patch('app.discover', return_value=(changed, None)), patch('app.messagebox') as dialogs:
            self.gui.enable_limited()
            dialogs.showwarning.assert_called_once()
        self.assertFalse(any(r['enabled'] for r in core.list_threads(self.db)))


if __name__ == '__main__': unittest.main()
