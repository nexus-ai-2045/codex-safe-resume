#!/usr/bin/env python3
"""Codex Desktop thread manager with experimental opt-in auto-resume monitoring."""
import argparse
import datetime
import json
import pathlib
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from core import connection, discover, sync, list_threads, enable, quota, mark, dry_run
from watch import once as monitor_once
from diagnostics import monitor_status


def tray_dependencies():
    try:
        import pystray
        from PIL import Image, ImageDraw
        return pystray, Image, ImageDraw
    except ImportError:
        return None


def create_tray_image(Image, ImageDraw):
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    painter = ImageDraw.Draw(image)
    painter.rounded_rectangle((5, 5, 59, 59), radius=15, fill='#245A91')
    painter.arc((17, 16, 48, 48), 40, 310, fill='white', width=6)
    painter.polygon([(48, 46), (47, 30), (35, 39)], fill='white')
    return image


class GUI:
    def __init__(self, enable_tray=True):
        self.root = tk.Tk()
        self.root.title('Codex 自動再開管理（安全モード）')
        self.root.geometry('1140x740')
        self.root.minsize(790, 525)
        self.db = connection()
        self.known = []
        self.rows = []
        self.tray = None
        self.tray_available = False
        self.closing = False
        self.monitor_queue = queue.Queue()
        self.monitor_running = False
        self.tray_requested = enable_tray
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value='すべて')
        self.status = tk.StringVar(value='Codexの会話を読み込み中…')
        self.details = tk.StringVar(value='左の一覧からチャットを選択してください。')
        self.tray_state = tk.StringVar(value='常駐アイコン：未接続')
        self.configure_style()
        self.build()
        self.search.trace_add('write', lambda *_: self.render_rows())
        self.filter.trace_add('write', lambda *_: self.render_rows())
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.refresh()
        if enable_tray:
            self.start_tray()
        self.root.after(30000, self.periodic)

    def monitor_tick(self):
        if self.closing: return
        # Only one monitor worker may be in flight. UI and tray refresh do not start workers.
        if not self.monitor_running:
            self.monitor_running = True
            def worker():
                try:
                    with connection() as db:
                        result = monitor_once(db)
                except Exception as exc:
                    result = {'state': 'monitor-error', 'reason': type(exc).__name__}
                finally:
                    # Always notify the GUI; even unexpected worker errors do not silently
                    # disable future monitoring cycles.
                    self.monitor_queue.put(result)
            threading.Thread(target=worker, daemon=True).start()
        self.root.after(30000, self.monitor_tick)

    def drain_monitor(self):
        if self.closing:return
        while not self.monitor_queue.empty():
            self.monitor_result(self.monitor_queue.get_nowait())
        self.root.after(1000, self.drain_monitor)

    def monitor_result(self, result):
        if self.closing:return
        state=result.get('state','unknown')
        if state == 'checked' and result.get('results'):
            self.refresh()
        if state != 'checked' or result.get('results'):
            self.write(monitor_status(result))
        self.monitor_running = False

    def configure_style(self):
        self.root.configure(background='#F3F6FA')
        style = ttk.Style(self.root)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure('Main.TFrame', background='#F3F6FA')
        style.configure('Panel.TFrame', background='white')
        style.configure('Heading.TLabel', background='#F3F6FA', foreground='#172B48', font=('TkDefaultFont', 19, 'bold'))
        style.configure('Sub.TLabel', background='#F3F6FA', foreground='#52657B', font=('TkDefaultFont', 10))
        style.configure('Panel.TLabel', background='white', foreground='#23354B')
        style.configure('Treeview', background='white', fieldbackground='white', foreground='#25364A', rowheight=37, font=('TkDefaultFont', 11), borderwidth=0)
        style.configure('Treeview.Heading', font=('TkDefaultFont', 10, 'bold'), padding=(9, 10), background='#E9EFF6', foreground='#233B58')
        style.map('Treeview', background=[('selected', '#D8E9FC')], foreground=[('selected', '#152F4E')])
        style.configure('Primary.TButton', padding=(13, 9))
        style.configure('Action.TButton', padding=(9, 8))

    def build(self):
        top = ttk.Frame(self.root, style='Main.TFrame', padding=(20, 16, 20, 8))
        top.pack(fill='x')
        ttk.Label(top, text='Codex 自動再開管理', style='Heading.TLabel').pack(anchor='w')
        ttk.Label(top, text='チャットを選んで自動再開を有効化  |  条件成立時は自動送信（実験版・誤動作リスクあり）', style='Sub.TLabel').pack(anchor='w', pady=(4, 0))
        bar = ttk.Frame(self.root, style='Main.TFrame', padding=(20, 12, 20, 10))
        bar.pack(fill='x')
        ttk.Label(bar, text='チャットを検索', style='Sub.TLabel').pack(side='left')
        ttk.Entry(bar, textvariable=self.search, width=36).pack(side='left', padx=(8, 16), fill='x', expand=True)
        ttk.Label(bar, text='絞り込み', style='Sub.TLabel').pack(side='left')
        ttk.Combobox(bar, state='readonly', width=15, values=['すべて', '有効のみ', '利用枠停止', '要確認', '完了'], textvariable=self.filter).pack(side='left', padx=(8, 12))
        ttk.Button(bar, text='チャット一覧を更新', style='Primary.TButton', command=self.refresh).pack(side='left')

        panel = ttk.Frame(self.root, style='Panel.TFrame', padding=1)
        panel.pack(fill='both', expand=True, padx=20, pady=(0, 10))
        table_frame = ttk.Frame(panel, style='Panel.TFrame')
        table_frame.pack(fill='both', expand=True)
        columns = ('title', 'project', 'status', 'auto', 'reset')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', selectmode='browse')
        for col, label, width, anchor, stretch in [
            ('title', 'チャット名', 460, 'w', True),
            ('project', 'プロジェクト', 180, 'w', True),
            ('status', '状態', 115, 'center', False),
            ('auto', '自動再開設定', 115, 'center', False),
            ('reset', 'リセット予定', 150, 'center', False),
        ]:
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, minwidth=85, anchor=anchor, stretch=stretch)
        self.tree.tag_configure('limited', background='#FFF1E8')
        self.tree.tag_configure('blocked', background='#FCEDEE')
        self.tree.tag_configure('completed', foreground='#7A8798')
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_row)
        self.tree.bind('<Double-1>', self.toggle_selected)

        detail_frame = ttk.Frame(panel, style='Panel.TFrame', padding=(14, 10))
        detail_frame.pack(fill='x')
        ttk.Label(detail_frame, text='選択したチャット', style='Panel.TLabel', font=('TkDefaultFont', 10, 'bold')).pack(anchor='w')
        ttk.Label(detail_frame, textvariable=self.details, style='Panel.TLabel', wraplength=1050, justify='left').pack(fill='x', pady=(4, 0))

        actions = ttk.Frame(self.root, style='Main.TFrame', padding=(20, 0, 20, 8))
        actions.pack(fill='x')
        ttk.Button(actions, text='選択したチャットを有効化', style='Primary.TButton', command=lambda: self.toggle(True)).pack(side='left', padx=(0, 7))
        ttk.Button(actions, text='無効化', style='Action.TButton', command=lambda: self.toggle(False)).pack(side='left', padx=(0, 7))
        ttk.Button(actions, text='完了にする', style='Action.TButton', command=lambda: self.finish('completed')).pack(side='left', padx=(0, 7))
        ttk.Button(actions, text='要確認にする', style='Action.TButton', command=lambda: self.finish('blocked')).pack(side='left', padx=(0, 7))
        ttk.Button(actions, text='再開予定を確認（実行しない）', style='Action.TButton', command=self.preview).pack(side='left')

        footer = ttk.Frame(self.root, style='Main.TFrame', padding=(20, 3, 20, 9))
        footer.pack(fill='x')
        ttk.Label(footer, textvariable=self.status, style='Sub.TLabel').pack(side='left')
        ttk.Label(footer, textvariable=self.tray_state, style='Sub.TLabel').pack(side='right')
        self.log = tk.Text(self.root, height=3, wrap='word', relief='flat', background='#EAF0F6', foreground='#344A61', font=('TkDefaultFont', 9))
        self.log.pack(fill='x', padx=20, pady=(0, 14))
        self.log.configure(state='disabled')

    def write(self, s):
        self.log.configure(state='normal')
        self.log.insert('end', str(s) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def refresh(self):
        selected = self.selected()
        self.known, warning = discover()
        if warning: self.write('読取注意: ' + warning)
        if self.known: sync(self.db, self.known)
        self.rows = list_threads(self.db)
        self.render_rows(selected)
        self.status.set(f'検出 {len(self.known)} 件 / 表示 {len(self.tree.get_children())} 件  |  30秒ごとに更新  |  自動再開: 実験機能')

    def render_rows(self, selected=None):
        selected = selected or self.selected()
        for item in self.tree.get_children(): self.tree.delete(item)
        live = {x['id']: x for x in self.known}
        query = self.search.get().casefold().strip()
        mode = self.filter.get()
        count = 0
        for row in self.rows:
            t = live.get(row['id'], {})
            title = row['title'] or '(タイトルなし)'
            project = pathlib.Path(row['cwd']).name if row['cwd'] else '—'
            if query and query not in (title + ' ' + project + ' ' + row['id']).casefold(): continue
            if mode == '有効のみ' and not row['enabled']: continue
            if mode == '利用枠停止' and not t.get('limited'): continue
            if mode == '要確認' and row['state'] != 'blocked': continue
            if mode == '完了' and row['state'] != 'completed': continue
            status = '利用枠停止' if t.get('limited') else {'idle':'待機中', 'waiting':'制限解除待ち', 'claimed':'再開確認中', 'completed':'完了', 'blocked':'要確認', 'dispatched':'再開要求済み', 'monitoring':'次の制限を監視中'}.get(row['state'], row['state'])
            reset = datetime.datetime.fromtimestamp(row['reset_at']).astimezone().strftime('%m/%d %H:%M') if row['reset_at'] else '—'
            tags = ('limited',) if t.get('limited') else (row['state'],)
            self.tree.insert('', 'end', iid=row['id'], values=(title, project, status, '有効' if row['enabled'] else '無効', reset), tags=tags)
            count += 1
        if selected and self.tree.exists(selected): self.tree.selection_set(selected)
        self.select_row()
        self.status.set(f'検出 {len(self.known)} 件 / 表示 {count} 件  |  30秒ごとに更新  |  自動再開: 実験機能')

    def select_row(self, _event=None):
        tid = self.selected()
        row = next((r for r in self.rows if r['id'] == tid), None)
        if not row:
            self.details.set('チャットを選択すると、正式なタイトルとスレッドIDがここに表示されます。')
            return
        self.details.set(f"{row['title']}\nプロジェクト: {row['cwd'] or '—'}\nスレッドID: {tid}")

    def toggle_selected(self, _event=None):
        tid = self.selected()
        row = next((r for r in self.rows if r['id'] == tid), None)
        if row: self.toggle(not bool(row['enabled']))

    def toggle(self, value):
        tid = self.selected()
        if not tid: return
        try:
            enable(self.db, tid, value)
            self.write(('自動再開対象に設定: ' if value else '自動再開対象から解除: ') + tid)
        except (ValueError, RuntimeError) as exc:
            messagebox.showwarning('変更できません', str(exc))
        self.refresh()

    def finish(self, status):
        tid = self.selected()
        if tid and messagebox.askyesno('確認', 'このチャットを「' + ('完了' if status == 'completed' else '要確認') + '」として記録しますか？'):
            mark(self.db, tid, status)
            self.refresh()

    def preview(self):
        items = dry_run(self.db, self.known)
        self.write('再開予定（送信なし）: ' + (', '.join(items) if items else '対象なし'))

    def periodic(self):
        if self.closing: return
        try: self.refresh()
        except Exception as exc: self.write('更新失敗: ' + str(type(exc).__name__))
        self.root.after(30000, self.periodic)

    def start_tray(self):
        deps = tray_dependencies()
        if not deps:
            self.tray_state.set('常駐アイコン：追加ライブラリが必要')
            self.write('タスクトレイを使うには pip install pystray pillow が必要です。')
            return
        pystray, Image, ImageDraw = deps
        def callback(func):
            return lambda icon, item: self.root.after(0, func)
        menu = pystray.Menu(
            pystray.MenuItem('ウィンドウを開く', callback(self.show_window), default=True),
            pystray.MenuItem('一覧を更新', callback(self.refresh)),
            pystray.MenuItem('終了', callback(self.quit_app)),
        )
        try:
            self.tray = pystray.Icon('codex_safe_resume', create_tray_image(Image, ImageDraw), 'Codex 自動再開管理', menu)
            # Tk owns the main thread; pystray background event loop owns its own backend.
            self.tray.run_detached()
            self.tray_available = True
            self.tray_state.set('常駐中（×でウィンドウを隠す）')
        except Exception as exc:
            self.tray_available = False
            self.tray = None
            self.tray_state.set('常駐アイコン：利用不可')
            self.write('トレイ初期化不可: ' + str(type(exc).__name__))

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_close(self):
        if self.tray_available:
            self.root.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        if self.closing: return
        self.closing = True
        if self.tray:
            try: self.tray.stop()
            except Exception: pass
        self.db.close()
        self.root.destroy()

    def run(self): self.root.mainloop()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('gui'); a.add_argument('--no-tray', action='store_true', help='常駐アイコンなしでGUIを起動')
    for command in ('scan', 'dry-run', 'list'): sub.add_parser(command)
    for command in ('enable', 'disable'):
        a = sub.add_parser(command); a.add_argument('id')
    a = sub.add_parser('quota-test'); a.add_argument('id'); a.add_argument('failure_turn'); a.add_argument('reset_at', type=int); a.add_argument('--limit', choices=['5h', 'weekly'], default='5h')
    a = sub.add_parser('mark'); a.add_argument('id'); a.add_argument('state', choices=['completed', 'blocked', 'idle'])
    args = p.parse_args()
    if args.cmd == 'gui': return GUI(enable_tray=not args.no_tray).run()
    with connection() as db:
        scanned, error = discover()
        if scanned: sync(db, scanned)
        if error: print('読み取りエラー:', error, file=sys.stderr)
        if args.cmd == 'scan': print(json.dumps(scanned, ensure_ascii=False, indent=2))
        elif args.cmd == 'list': print(json.dumps(list_threads(db), ensure_ascii=False, indent=2))
        elif args.cmd == 'dry-run': print(json.dumps(dry_run(db, scanned), ensure_ascii=False))
        elif args.cmd in ('enable', 'disable'): enable(db, args.id, args.cmd == 'enable')
        elif args.cmd == 'quota-test': quota(db, args.id, args.failure_turn, args.reset_at, args.limit)
        elif args.cmd == 'mark': mark(db, args.id, args.state)


if __name__ == '__main__': main()