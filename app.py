#!/usr/bin/env python3
"""Codex Desktop thread manager with experimental opt-in auto-resume monitoring."""
import sqlite3
import datetime
import json
import pathlib
import sys
import threading
import queue
import time
import re
from contextlib import closing
from app_icons import create_tray_image
from mac_tray import snapshot as mac_tray_snapshot
from thread_navigation import open_thread as request_chat_open
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, font as tkfont
    from quota_widgets import QuotaCards
except ImportError:
    tk = ttk = messagebox = filedialog = tkfont = QuotaCards = None
from core import connection, discover, sync, list_threads, enable, quota, mark, dry_run, check_id, gui_lease
from watch import once as monitor_once
from diagnostics import monitor_status, JapaneseParser
from connection_settings import (configure as configure_connection, load as connection_options,
                                 enabled as connection_enabled, prepare as prepare_connection,
                                 validate_executable)
from cli_security import validate_location
from usage_client import UsageReadError, read_limits, summarize_limits
from plan_metadata import PLAN_LABELS
from quota_presentation import build_usage_view
from desktop_bridge import DesktopBridge
from auto_watch import (initialize as initialize_auto_watch, status as auto_watch_status,
                        set_enabled as set_auto_watch)


def start_auto_connection(db, *, restart_acknowledged):
    raise ValueError("起動済みCodexとの同一接続を確認する方式は未対応です")


def prepare_queue_connection():
    from queue_bridge import prepare_config
    return prepare_config()


def save_queue_connection(db, config, *, before_store=None):
    from connection_settings import configure_queue
    configure_queue(db, config, manual_exclusion_acknowledged=True, before_store=before_store)


def reconcile_pending_shutdown(home):
    from core import monitor_lease
    from queue_bridge import QueueBridge
    from queue_reconcile import reconcile
    from queue_state import pending
    with closing(connection(home)) as db:
        set_auto_watch(db, False)
        with monitor_lease(db) as acquired:
            if not acquired:
                return False
            rows = pending(db)
            if not rows:
                return True
            if len(rows) > 8:
                return False
            with QueueBridge(config=connection_options(db), timeout=3, for_pending=True) as bridge:
                reconcile(db, bridge)
            return not pending(db)


def tray_dependencies():
    try:
        import pystray
        from PIL import Image, ImageDraw
        return pystray, Image, ImageDraw
    except ImportError:
        return None


def usage_executable(config):
    """表示用のpinを優先し、更新後は署名検証した固定公式bundleだけを使う。"""
    if config:
        try:
            return validate_executable(config, require_confirmation=False)
        except (ValueError, OSError, RuntimeError):
            if sys.platform != 'darwin':
                raise
    if sys.platform == 'darwin':
        from queue_platform import DARWIN_TEAM, verify_darwin_cli_files
        bundle = pathlib.Path('/Applications/ChatGPT.app')
        bundled = bundle / 'Contents' / 'Resources' / 'codex-cli' / 'bin' / 'codex'
        verify_darwin_cli_files(bundle, [bundled], DARWIN_TEAM)
        return bundled
    raise ValueError('公式CLIを選択してください')


def read_usage_status(config, *, cancel=None):
    """表示用の取得元だけを照合。既存共有接続への読取り以外は行わない。"""
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise UsageReadError('利用枠の読取りを中止しました')

    check_cancel()
    peer = None
    state = 'not_configured'
    if config:
        state = 'unavailable'
        try:
            # 古いpinで共有接続へ触れず、表示用bundleの重い検証は最後に一度だけ。
            validate_executable(config, require_confirmation=False)
            check_cancel()
            if config.get('mode') == 'queue':
                from queue_bridge import QueueBridge
                selected = QueueBridge(config=config, for_pending=True, timeout=3)
            else:
                selected = DesktopBridge(config=config, transport_only=True, timeout=3)
            with selected as bridge:
                check_cancel()
                if config.get('mode') == 'queue' and bridge.capabilities().get('desktop_binding') is not True:
                    raise ValueError('起動中のCodexを確認できません')
                check_cancel()
                peer = bridge.quota()
                state = 'read'
        except Exception:
            pass
    check_cancel()
    # 共有接続の確認中にCLIが更新されても、古いpin検証で起動しない。
    executable = usage_executable(config)
    check_cancel()
    try:
        result = read_limits(executable, peer_reply=peer, cancel=cancel)
    except UsageReadError:
        check_cancel()
        if peer is None:
            raise
        # CLI側が未取得でも、読めた共有接続の値をCLIの値と偽らず表示する。
        result = dict(plan_type=None, peer_status='read',
                      peer_limits=summarize_limits(peer), account_match=None)
    result['desktop_connection'] = ('invalid' if state == 'read'
                                    and result.get('peer_status') != 'read' else state)
    return result


def display_connection_ready(config):
    """load()が今回確認した結果を表示にだけ再利用。送信許可には使わない。"""
    if config.get('mode') == 'managed' and '_runtime_attested' in config:
        return config['_runtime_attested'] is True
    return connection_enabled(config)


def monitor_rows_snapshot(rows):
    """受領の表示に関わる追跡項目だけを、並び順や観測時刻によらず保存する。"""
    fields = ('state', 'enabled', 'generation', 'auto_excluded',
              'failure_turn', 'resume_turn', 'reset_at')
    return {row['id']: tuple(row.get(key) for key in fields) for row in rows}


def connection_text(config, reading=None):
    """本人確認と実測を分離。ID・メール・配置先・未対応プラン値は表示しない。"""
    reading = reading or {}
    def label(value):
        return PLAN_LABELS.get(value, '不明') if isinstance(value, str) else '不明'
    cli_plan = label(reading.get('plan_type'))
    peer_plan = label((reading.get('peer_limits') or {}).get('plan_type'))
    operator = '済' if config.get('desktop_confirmed') is True else '未'
    state = reading.get('desktop_connection')
    shared = {'read': '読取り成功', 'unavailable': '確認できず',
              'invalid': '応答未確認', 'not_configured': '未設定'}.get(
                  state, '未検査' if config else '未設定')
    match = reading.get('account_match')
    account = '未照合'
    if state == 'read' and reading.get('peer_status') == 'read':
        account = ('一致（取得時点）' if match is True else
                   '不一致' if match is False else '情報不足')
    sending = '設定済（対象は別途選択）' if display_connection_ready(config) else '停止'
    disagreement = '【区分相違】' if cli_plan != '不明' and peer_plan != '不明' and cli_plan != peer_plan else ''
    return (f'プラン: {cli_plan}（CLI）/ {peer_plan}（共有接続）{disagreement} | Desktop本人確認: {operator}\n'
            f'共有接続: {shared} | アカウント: {account} | 自動送信: {sending}')


def usage_unknown_text(reason):
    return f'5時間枠: 未確認\n1週間枠: 未確認\n{reason}'


def usage_text(result, *, today=None, now=None):
    """カードと同じモデルによる、詳細・互換表示用のテキスト。"""
    view = build_usage_view(result, now=now, today=today)
    return usage_view_text(view)


def usage_view_text(view):
    lines = []
    for card in view['cards']:
        text = f"{card['name']}: {card['summary']}"
        if card['reset_text']:
            text += '（' + card['reset_text'] + '）'
        lines.append(text + ' / ' + card['details'])
    return '\n'.join(lines + [view['availability_text'], view['updated_text']])


class ScrollViewport:
    def __init__(self, root):
        self.focus_target = None
        self.fresh_focus = False
        self.focus_anchor = None
        self.focus_update = None
        self.resize_pending = None
        self.layout_size = None
        self.canvas = tk.Canvas(root, background='#F3F6FA', highlightthickness=0,
                                borderwidth=0)
        self.scrollbar = ttk.Scrollbar(root, orient='vertical',
                                       command=self.canvas.yview, takefocus=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.scrollbar.grid(row=0, column=1, sticky='ns')
        self.body = ttk.Frame(self.canvas, style='Main.TFrame')
        self.content = self.canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.canvas.bind('<Configure>', self.schedule_resize, add='+')
        self.body.bind('<Configure>', self.schedule_resize, add='+')
        root.bind('<Configure>', self.schedule_resize, add='+')
        root.bind('<FocusIn>', self.focus_changed, add='+')

    def schedule_resize(self, _event=None):
        if self.resize_pending is None:
            self.resize_pending = self.canvas.after_idle(self.resize)

    def resize(self, _event=None):
        self.resize_pending = None
        width = max(1, self.canvas.winfo_width())
        visible = self.canvas.winfo_height()
        height = max(visible, self.body.winfo_reqheight())
        actual = self.body.winfo_height()
        self.discard_scrolled_focus()
        if self.layout_size == (width, height, visible, actual):
            return
        self.layout_size = (width, height, visible, actual)
        first, last = self.canvas.yview()
        at_bottom = first > 0 and last >= 1
        self.canvas.itemconfigure(self.content, width=width, height=height)
        # Native Tk can temporarily allocate more than the requested window
        # height. Keep that content reachable without fixing the window at
        # its previous size; the next Configure can shrink the region again.
        self.canvas.configure(scrollregion=(0, 0, width, max(height, actual)))
        if at_bottom and not self.fresh_focus:
            # Keep an explicit scroll to the end while wrapped labels settle.
            # A queued check of the old focused control must not pull it back.
            if self.focus_update is not None:
                self.canvas.after_cancel(self.focus_update)
                self.focus_update = None
            self.canvas.yview_moveto(1)
            return
        # Wrapping can change the focused control's position after FocusIn.
        # Restore visibility when dimensions settle, not on manual scrolling.
        if self.focus_target is not None and self.focus_update is None:
            self.focus_update = self.canvas.after(30, self.restore_focus_visibility)

    def discard_scrolled_focus(self):
        if (self.focus_target is not None and not self.fresh_focus
                and self.focus_anchor is not None
                and abs(self.canvas.canvasy(0) - self.focus_anchor) > 1):
            # A later native Configure must not undo a user's scroll away
            # from the previously focused control.
            self.focus_target = None
            self.focus_anchor = None
            if self.focus_update is not None:
                self.canvas.after_cancel(self.focus_update)
                self.focus_update = None

    def restore_focus_visibility(self):
        self.focus_update = None
        self.discard_scrolled_focus()
        self.fresh_focus = False
        if self.focus_target is not None:
            self.ensure_visible(self.focus_target)

    def focus_changed(self, event):
        self.fresh_focus = True
        self.focus_target = event.widget
        self.ensure_visible(event.widget)
        if self.focus_update is None:
            self.focus_update = self.canvas.after(30, self.restore_focus_visibility)

    def ensure_visible(self, widget):
        ancestor = widget
        while ancestor is not None and ancestor is not self.body:
            ancestor = getattr(ancestor, 'master', None)
        if ancestor is None:
            return
        if not widget.winfo_exists():
            return
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + widget.winfo_height()
        first = self.canvas.canvasy(0)
        visible = self.canvas.winfo_height()
        region = self.canvas.tk.splitlist(self.canvas.cget('scrollregion'))
        height = float(region[3]) - float(region[1]) if len(region) == 4 else self.body.winfo_height()
        if top < first:
            self.canvas.yview_moveto(top / max(1, height))
        elif bottom > first + visible:
            self.canvas.yview_moveto((bottom - visible) / max(1, height))
        self.focus_anchor = self.canvas.canvasy(0)


class GUI:
    def __init__(self, enable_tray=True):
        if tk is None:
            raise RuntimeError('画面ライブラリが必要です')
        self.root = tk.Tk()
        self.root.title('Codex Safe Resume')
        self.configure_window()
        self.configure_window_icon()
        self.db = connection()
        self.simple_mode = True
        self.initialize_auto_policy()
        self.known = []
        self.rows = []
        self.tray = None
        self.tray_available = False
        self.window_hidden = False
        self.closing = False
        self.monitor_queue = queue.Queue()
        self.monitor_running = False
        self.monitor_thread = None
        self.usage_running = False
        self.usage_thread = None
        self.usage_cancel = None
        self.usage_queue = queue.Queue(maxsize=1)
        self.navigation_pending = False
        self.navigation_thread = None
        self.navigation_cancel = threading.Event()
        self.navigation_results = queue.Queue(maxsize=1)
        self.queue_setup_running = False
        self.queue_setup_thread = None
        self.queue_setup_results = queue.Queue(maxsize=1)
        self.queue_setup_token = 0
        self.queue_setup_context = None
        self.tray_requested = enable_tray
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value='すべて')
        self.status = tk.StringVar(value='Codexの会話を読み込み中…')
        self.details = tk.StringVar(value='利用枠で止まったチャットを自動検出します。行を選ぶと対象を確認できます。')
        self.auto_status = tk.StringVar(value='自動再開 OFF')
        self.plan_note = tk.StringVar(value='プランを確認中…')
        self.usage_status = tk.StringVar(value=usage_unknown_text('読取り準備中（再開送信とは独立した表示です）'))
        self.tray_state = tk.StringVar(value=f'{self.tray_location()}：未接続')
        self.configure_style()
        self.build()
        self.search.trace_add('write', lambda *_: self.render_rows())
        self.filter.trace_add('write', lambda *_: self.render_rows())
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.configure_reopen()
        self.root.bind('<Map>', self.sync_tray_window, add='+')
        self.root.bind('<Unmap>', self.sync_tray_window, add='+')
        self.refresh()
        self.show_window()
        if enable_tray:
            self.root.after_idle(self.start_tray)
        self.root.after(30000, self.periodic)
        self.root.after(8000, self.monitor_tick)
        self.root.after(1000, self.drain_monitor)
        self.root.after(1500, self.usage_tick)
        self.root.after(1000, self.display_tick)
        self.root.after(500, self.drain_usage)

    def initialize_auto_policy(self):
        initialize_auto_watch(self.db)

    def invalidate_monitor_results(self, ids=None, *, rows_changed=False):
        """表示用の受領だけを失効させる。送信側の証明・試行履歴には触れない。"""
        self.monitor_display_revision = getattr(self, 'monitor_display_revision', 0) + 1
        if not rows_changed:
            # 接続・方針・検出などの失効は、行の一致だけで取り消さない。
            self.monitor_context_revision = getattr(self, 'monitor_context_revision', 0) + 1
        result = getattr(self, 'last_monitor_result', {})
        if ids is None:
            self.last_monitor_result = {}
        else:
            self.last_monitor_result = dict(result, results=[(tid, value)
                                             for tid, value in result.get('results', []) if tid not in ids])
        self.last_monitor_at = None

    def reload_rows(self):
        previous = monitor_rows_snapshot(getattr(self, 'rows', []))
        self.rows = list_threads(self.db)
        current = monitor_rows_snapshot(self.rows)
        changed = {tid for tid in previous.keys() | current.keys()
                   if previous.get(tid) != current.get(tid)}
        if changed:
            self.invalidate_monitor_results(changed, rows_changed=True)

    def refresh_auto_status(self, *, config=None):
        policy = auto_watch_status(self.db) or {'enabled': False}
        config = connection_options(self.db) if config is None else config
        previous = getattr(self, 'display_auto_config', None)
        previous_policy = getattr(self, 'display_auto_policy', None)
        self.display_auto_policy, self.display_auto_config = dict(policy), dict(config)
        if previous is not None and previous != config:
            self.invalidate_monitor_results()
            self.clear_usage_display(config)
        elif previous_policy is not None and previous_policy != policy:
            self.invalidate_monitor_results()
        self.display_auto_ready = display_connection_ready(config) if policy['enabled'] else False
        self.render_auto_status(policy, config, self.display_auto_ready)

    def render_auto_status(self, policy, config, ready):
        """同じ表示根拠から文言と操作を一緒に更新する。ここでは再証明しない。"""
        if not hasattr(self, 'auto_status'):
            return
        live = {item['id']: item for item in getattr(self, 'known', [])}
        blocked_ids = {row['id'] for row in getattr(self, 'rows', [])
                       if row.get('state') == 'blocked' and self.is_visible_row(row, live)}
        action = 'connection'
        setup_running = getattr(self, 'queue_setup_running', False)
        if setup_running:
            text = '接続確認中：画面を操作できます。中止すると自動再開はOFFになります'
        elif not policy['enabled']:
            text = '停止中：自動再開は無効です'
        elif blocked_ids:
            text = '要確認：停止した対象があります。要確認のチャットを確認してください'
            action = 'targets'
        elif config.get('_connection_phase') == 'failed':
            text = '要確認：接続できません。接続の確認・修復を行ってください'
        elif not ready:
            text = '接続確認中：Desktopとの接続を準備しています'
        else:
            result = getattr(self, 'last_monitor_result', {})
            received_at = getattr(self, 'last_monitor_at', None)
            matching = (received_at is not None
                        and getattr(self, 'last_monitor_policy', None) == policy
                        and getattr(self, 'last_monitor_config', None) == config
                        and 0 <= time.monotonic() - received_at <= 90)
            state = result.get('state') if matching else None
            blocked = matching and any(tid in blocked_ids and value == 'blocked'
                                       for tid, value in result.get('results', []))
            if not matching:
                text = '接続確認中：最新の監視結果を確認しています'
            elif state == 'empty' and not blocked:
                text = '監視中：対象のチャットはまだありません'
            elif state in ('checked', 'quota-wait') and not blocked:
                text = ('監視中：利用枠の回復を待っています' if state == 'quota-wait'
                        else '監視中：利用枠で止まったチャットを確認しています')
            elif state in ('offline', 'unavailable', 'unsupported-transport', 'monitor-error') or blocked:
                text = ('要確認：停止した対象があります。要確認のチャットを確認してください' if blocked else
                        '要確認：監視結果を確認できません。接続を確認してください')
                if blocked:
                    action = 'targets'
            elif state == 'disabled':
                text = '停止中：自動再開は無効です'
            elif state == 'busy':
                text = '接続確認中：別の監視処理が確認しています'
            else:
                # 新しい受領の未知・欠落した状態は、受領前や期限切れと区別する。
                text = '要確認：監視結果を確認できません'
        if hasattr(self, 'connection_action'):
            self.connection_action.configure(
                text='要確認のチャットを表示' if action == 'targets' else '接続の確認・修復…',
                command=self.show_review_targets if action == 'targets' else self.reconnect_auto_watch)
            self.connection_action.grid() if text.startswith('要確認') else self.connection_action.grid_remove()
        self.auto_status.set(text)
        if hasattr(self, 'auto_button'):
            self.auto_button.configure(text='接続確認を中止・自動再開をOFF' if setup_running else
                                       '自動再開をOFF' if policy['enabled'] else '自動再開をON')

    def show_review_targets(self):
        self.view_filter.set('確認が必要')
        self.search.set('')
        self.filter.set('すべて')
        self.render_rows()

    def toggle_auto_watch(self):
        policy = auto_watch_status(self.db) or {'enabled': False}
        if policy['enabled'] or getattr(self, 'queue_setup_running', False):
            context = getattr(self, 'queue_setup_context', None)
            if context is not None:
                context['cancelled'].set()
            set_auto_watch(self.db, False)
            self.reload_rows()
            self.render_rows()
            return
        config = connection_options(self.db)
        try:
            connected = connection_enabled(config) and (
                config.get('mode') in ('managed', 'queue') or getattr(self, 'desktop_read_state', None) == 'read')
            if not connected:
                self.request_desktop_connection(enable_auto_resume=True)
                return
            set_auto_watch(self.db, True)
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            self.auto_status.set('接続を開始できませんでした。詳細から状態を確認できます。')
            return
        self.reload_rows()
        self.render_rows()
        self.request_usage()

    @staticmethod
    def queue_setup_signature(db):
        return connection_options(db), auto_watch_status(db)

    def _queue_setup_current(self, context, *, config=None):
        if (getattr(self, 'closing', False) or context['cancelled'].is_set()
                or getattr(self, 'queue_setup_context', None) is not context):
            return False
        current_config, current_policy = self.queue_setup_signature(self.db)
        expected_config = context['signature'][0] if config is None else config
        return current_config == expected_config and current_policy == context['signature'][1]

    def _queue_setup_finish(self):
        context = getattr(self, 'queue_setup_context', None)
        if context is not None:
            context['cancelled'].set()
        self.queue_setup_running = False
        self.queue_setup_context = None

    def _queue_setup_worker(self, context, phase, config=None):
        result = None
        try:
            if not context['cancelled'].is_set():
                if phase == 'prepared':
                    result = prepare_queue_connection()
                else:
                    with closing(connection(context['home'])) as db:
                        def before_store():
                            return (not context['cancelled'].is_set() and not getattr(self, 'closing', False)
                                    and self.queue_setup_signature(db) == context['signature'])
                        save_queue_connection(db, config, before_store=before_store)
                        result = connection_options(db)
        except Exception:
            result = None
        context['results'].put_nowait((context['token'], phase, result))

    def _queue_setup_launch(self, context, phase, config=None):
        self.queue_setup_thread = threading.Thread(target=self._queue_setup_worker,
            args=(context, phase, config), daemon=False, name='queue-connection-setup')
        self.queue_setup_thread.start()
        self.root.after(100, self.drain_queue_setup)

    def request_desktop_connection(self, *, enable_auto_resume=False):
        thread = getattr(self, 'queue_setup_thread', None)
        context = getattr(self, 'queue_setup_context', None)
        if (context is not None and context['cancelled'].is_set()
                and thread is not None and not thread.is_alive()):
            self._queue_setup_finish()
        if (getattr(self, 'closing', False) or getattr(self, 'queue_setup_running', False)
                or (thread is not None and thread.is_alive())):
            return False
        try:
            raw = self.db.execute('PRAGMA database_list').fetchone()[2]
            if not isinstance(raw, str) or not raw:
                raise ValueError('state_path')
            self.queue_setup_token = getattr(self, 'queue_setup_token', 0) + 1
            context = {'token': self.queue_setup_token, 'cancelled': threading.Event(),
                       'results': queue.Queue(maxsize=1), 'home': pathlib.Path(raw).resolve().parent,
                       'signature': self.queue_setup_signature(self.db),
                       'enable': enable_auto_resume is True}
            self.queue_setup_context = context
            self.queue_setup_results = context['results']
            self.queue_setup_running = True
            self._queue_setup_launch(context, 'prepared')
            if hasattr(self, 'auto_status'):
                self.auto_status.set('接続確認中：画面を操作できます。自動再開の切替で確認を取り消せます。')
            return True
        except Exception:
            self._queue_setup_finish()
            messagebox.showinfo('Codexの接続',
                                '接続を確認できません。自動再開は開始していません。', parent=self.root)
            return False

    def drain_queue_setup(self):
        context = getattr(self, 'queue_setup_context', None)
        if context is None:
            return
        if getattr(self, 'closing', False):
            context['cancelled'].set()
            return
        try:
            token, phase, result = context['results'].get_nowait()
        except queue.Empty:
            if self.queue_setup_running:
                self.root.after(100, self.drain_queue_setup)
            return
        if token != context['token']:
            self._queue_setup_finish()
            return
        expected = result if phase == 'saved' and isinstance(result, dict) else None
        if not self._queue_setup_current(context, config=expected):
            self._queue_setup_finish()
            return
        if not isinstance(result, dict):
            self._queue_setup_finish()
            messagebox.showinfo('接続を確認できません',
                                '確認中に接続状態が変わったか、接続を確認できません。自動再開は開始していません。',
                                parent=self.root)
            return
        if phase == 'prepared':
            message = ('起動中のCodexとの接続を確認しました。利用枠で止まった対象を確認し、'
                       '回復後に「続けて」を一度だけ送ります。\n'
                       '対象チャットを手動操作する前は自動再開をOFFにしてください。'
                       '送信済みの要求は到達状況を確認し、取り消せる待機項目だけを取り消します。')
            if (not messagebox.askyesno('自動再開の操作ルール', message, parent=self.root, default='no')
                    or not self._queue_setup_current(context)):
                self._queue_setup_finish()
                return
            try:
                self._queue_setup_launch(context, 'saved', result)
            except Exception:
                self._queue_setup_finish()
                messagebox.showinfo('接続を確認できません', '保存処理を開始できません。自動再開は開始していません。',
                                    parent=self.root)
            return
        try:
            if context['enable'] and self._queue_setup_current(context, config=result):
                set_auto_watch(self.db, True, before_change=lambda: self._queue_setup_current(context, config=result))
            self._queue_setup_finish()
            self.update_connection_note()
            self.reload_rows()
            self.render_rows()
            self.refresh_auto_status()
            self.request_usage()
        except (ValueError, RuntimeError, OSError, sqlite3.Error):
            self._queue_setup_finish()
            self.auto_status.set('接続を開始できませんでした。詳細から状態を確認できます。')

    def reconnect_auto_watch(self):
        self.request_desktop_connection()

    def poll_desktop_connection(self):
        if self.closing:
            return
        poll_again = True
        try:
            config = connection_options(self.db)
            changed = config != getattr(self, 'display_auto_config', None)
            self.refresh_auto_status(config=config)
            if changed:
                self.reload_rows()
                self.render_rows(config=config)
            if config.get('_connection_phase') == 'ready':
                self.request_usage()
            poll_again = config.get('_connection_phase') == 'connecting'
        except Exception:
            self.write('接続状態を確認できません。次回再確認します')
        finally:
            if poll_again and not self.closing:
                self.root.after(1000, self.poll_desktop_connection)

    def toggle_details(self):
        self.simple_mode = not self.simple_mode
        for widget in self.advanced_widgets:
            widget.grid_remove() if self.simple_mode else widget.grid()
        self.tree.configure(displaycolumns=('title', 'auto', 'status', 'reset') if self.simple_mode
                            else ('title', 'project', 'status', 'auto', 'reset'))
        self.render_rows()

    def configure_window(self, window=None, *, max_width=1060, max_height=700):
        window = self.root if window is None else window
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        width = min(max_width, max(1, screen_width - 48))
        height = min(max_height, max(1, screen_height - 100))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2 - 20)
        window.geometry(f'{width}x{height}+{x}+{y}')
        window.minsize(min(620, width), min(420, height))

    def configure_window_icon(self):
        if sys.platform != 'win32':
            return
        from PIL import Image, ImageDraw, ImageTk
        self.window_icon = ImageTk.PhotoImage(create_tray_image(Image, ImageDraw), master=self.root)
        self.root.iconphoto(True, self.window_icon)

    def configure_reopen(self):
        if sys.platform == 'darwin':
            self.root.createcommand('tk::mac::ReopenApplication', self.show_window)
            self.root.createcommand('tk::mac::Quit', self.quit_app)

    def tray_location(self):
        return 'メニューバー' if sys.platform == 'darwin' else 'タスクトレイ'

    def monitor_tick(self):
        if self.closing: return
        try:
            # 同時に動かす監視は一つだけ。画面・常駐アイコンの更新からは起動しない。
            if not self.monitor_running:
                self.monitor_running = True
                try:
                    observed_policy = auto_watch_status(self.db)
                    observed_config = connection_options(self.db)
                    self.refresh_auto_status(config=observed_config)
                    observed_revision = getattr(self, 'monitor_display_revision', 0)
                    observed_context = getattr(self, 'monitor_context_revision', 0)
                    def worker():
                        try:
                            with connection() as db:
                                result = monitor_once(db)
                                # 同じ接続で監視直後の行を保存し、監視自身の遷移と受領待ちの変更を区別する。
                                result['_ui_rows'] = monitor_rows_snapshot(list_threads(db))
                        except Exception as exc:
                            result = {'state': 'monitor-error', 'reason': type(exc).__name__}
                        finally:
                            # 想定外の失敗でも画面へ通知し、次回の監視を再開できるようにする。
                            result['_ui_policy'] = observed_policy
                            result['_ui_config'] = observed_config
                            result['_ui_revision'] = observed_revision
                            result['_ui_context_revision'] = observed_context
                            result['_ui_finished_at'] = time.monotonic()
                            self.monitor_queue.put(result)
                    self.monitor_thread = threading.Thread(target=worker, daemon=False,
                                                           name='resume-monitor')
                    self.monitor_thread.start()
                except Exception:
                    self.monitor_running = False
                    self.write('監視を開始できません。次回再確認します')
        finally:
            if not self.closing:
                self.root.after(30000, self.monitor_tick)

    def drain_monitor(self):
        if self.closing:return
        try:
            while not self.monitor_queue.empty():
                self.monitor_result(self.monitor_queue.get_nowait())
        except Exception:
            self.write('監視結果を反映できません。次回再確認します')
        finally:
            if not self.closing:
                self.root.after(1000, self.drain_monitor)

    def monitor_result(self, result):
        if self.closing:return
        try:
            state=result.get('state','unknown')
            # 検出・同期や送信結果を待たずに、保存済みの行と利用枠の日時を読み直す。
            self.reload_rows()
            revision = getattr(self, 'monitor_display_revision', 0)
            if '_ui_rows' in result:
                current = (result['_ui_rows'] == monitor_rows_snapshot(self.rows)
                           and result.get('_ui_context_revision', 0)
                           == getattr(self, 'monitor_context_revision', 0))
            else:
                # 例外などで監視直後の行を保存できなかった受領は、開始時の表示世代も必須とする。
                current = result.get('_ui_revision', 0) == revision
            if current:
                # 監視自身の変更だけなら、読み直した行の表示世代に受領を合わせる。
                result['_ui_revision'] = revision
                self.last_monitor_result = result
                self.last_monitor_policy = result.get('_ui_policy')
                self.last_monitor_config = result.get('_ui_config')
                self.last_monitor_at = result.get('_ui_finished_at')
            self.render_rows()
            if state != 'checked' or result.get('results'):
                self.write(monitor_status(result))
        finally:
            self.monitor_running = False

    def refresh_all(self):
        self.refresh()
        self.request_usage()

    def usage_tick(self):
        if self.closing:
            return
        try:
            if getattr(self, 'last_usage_result', None) is not None:
                self.render_usage()
            self.refresh_auto_status()
            self.request_usage()
        except Exception:
            self.write('利用枠の確認を開始できません。次回再確認します')
        finally:
            if not self.closing:
                self.root.after(60000, self.usage_tick)

    def display_tick(self):
        if self.closing:
            return
        try:
            self.render_usage()
            if hasattr(self, 'display_auto_policy'):
                self.render_auto_status(self.display_auto_policy, self.display_auto_config,
                                        self.display_auto_ready)
                if getattr(self, 'last_usage_result', None) is not None:
                    self.render_connection_reading(self.display_auto_config)
        except Exception:
            self.write('表示を更新できません。次回再確認します')
        finally:
            if not self.closing:
                self.root.after(1000, self.display_tick)

    def render_usage(self):
        result = getattr(self, 'last_usage_result', None)
        view = build_usage_view(result, failed=getattr(self, 'usage_failed', False))
        if hasattr(self, 'quota_cards'):
            self.quota_cards.render(view)
        if hasattr(self, 'usage_updated'):
            self.usage_updated.set(view['updated_text'] + ' | ' + view['availability_text'])
        if hasattr(self, 'usage_status'):
            self.usage_status.set(usage_view_text(view))

    def request_usage(self):
        if self.closing or self.usage_running:
            return
        config = connection_options(self.db)
        signature = json.dumps(config, sort_keys=True)
        self.usage_running = True
        cancel = self.usage_cancel = threading.Event()
        def worker():
            try:
                result = read_usage_status(config, cancel=cancel)
            except Exception:
                result = None
            try:
                self.usage_queue.put_nowait((signature, result))
            except queue.Full:
                pass
        # 通常終了でもowned RPCのfinally回収を完了させる。
        try:
            self.usage_thread = threading.Thread(target=worker, daemon=False, name='usage-display')
            self.usage_thread.start()
        except Exception:
            self.usage_running = False
            self.usage_failed = True
            self.update_connection_note(historical=True)
            self.render_usage()
            self.usage_status.set(usage_unknown_text('利用枠の読取りを開始できません'))

    def drain_usage(self):
        if self.closing:
            return
        try:
            try:
                signature, result = self.usage_queue.get_nowait()
            except queue.Empty:
                pass
            else:
                self.usage_running = False
                config = connection_options(self.db)
                current = json.dumps(config, sort_keys=True)
                if current != signature:
                    self.update_connection_note(config=config)
                    self.usage_status.set(usage_unknown_text('取得元が変わりました。利用枠を再取得します'))
                    self.request_usage()
                elif result is None:
                    self.usage_failed = True
                    self.update_connection_note(historical=True, config=config)
                    self.render_usage()
                    self.usage_status.set(usage_unknown_text('利用枠を取得できません。前回取得値は更新待ちです'))
                else:
                    self.update_connection_note(result, config=config)
                    self.render_usage()
        except Exception:
            self.write('利用枠の取得結果を反映できません。次回再確認します')
        finally:
            if not self.closing:
                self.root.after(500, self.drain_usage)

    def configure_style(self):
        self.root.configure(background='#F3F6FA')
        style = ttk.Style(self.root)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure('Main.TFrame', background='#F3F6FA')
        style.configure('Panel.TFrame', background='white')
        style.configure('Heading.TLabel', background='#F3F6FA', foreground='#172B48', font=('TkDefaultFont', 17, 'bold'))
        style.configure('Sub.TLabel', background='#F3F6FA', foreground='#52657B', font=('TkDefaultFont', 10))
        style.configure('Panel.TLabel', background='white', foreground='#23354B')
        style.configure('Treeview', background='white', fieldbackground='white', foreground='#25364A', rowheight=32, font=('TkDefaultFont', 11), borderwidth=0)
        style.configure('Treeview.Heading', font=('TkDefaultFont', 10, 'bold'), padding=(7, 6), background='#E9EFF6', foreground='#233B58')
        style.map('Treeview', background=[('selected', '#D8E9FC')], foreground=[('selected', '#152F4E')])
        style.configure('Primary.TButton', padding=(8, 5))
        style.configure('Action.TButton', padding=(8, 5))

    def build(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.viewport = ScrollViewport(self.root)
        body = self.viewport.body
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1, minsize=80)
        self.wrap_labels = []
        top = ttk.Frame(body, style='Main.TFrame', padding=(16, 8, 16, 2))
        top.grid(row=0, column=0, sticky='ew')
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text='Codex Safe Resume', style='Heading.TLabel').grid(row=0, column=0, sticky='w')
        self.auto_button = ttk.Button(top, text='自動再開をON', style='Primary.TButton', command=self.toggle_auto_watch)
        self.auto_button.grid(row=0, column=1, sticky='e', padx=(8, 0))
        details_button = ttk.Menubutton(top, text='設定')
        details_button.grid(row=0, column=2, padx=(8, 0))
        details_menu = tk.Menu(details_button, tearoff=False)
        details_menu.add_command(label='接続を確認・再設定（同意が必要）…', command=self.reconnect_auto_watch)
        details_menu.add_command(label='利用枠の詳細（送信しない）', command=self.show_usage_details)
        details_menu.add_command(label='一覧と利用枠を再取得（送信しない）', command=self.refresh_all)
        details_menu.add_separator()
        details_menu.add_command(label='アプリを終了', command=self.quit_app)
        details_button.configure(menu=details_menu)
        plan = ttk.Label(top, textvariable=self.plan_note, style='Sub.TLabel', wraplength=580)
        plan.grid(row=1, column=0, columnspan=3, sticky='ew', pady=(6, 0))
        self.quota_cards = QuotaCards(top)
        self.quota_cards.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(6, 2))
        self.usage_updated = tk.StringVar(value='未取得')
        updated = ttk.Label(top, textvariable=self.usage_updated, style='Sub.TLabel', wraplength=580)
        updated.grid(row=3, column=0, columnspan=3, sticky='ew')
        automatic = ttk.Label(top, textvariable=self.auto_status, style='Sub.TLabel', wraplength=580)
        automatic.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(4, 0))
        help_note = ttk.Label(top, text='利用枠で止まったチャットを自動で対象にします。個別設定は不要です。',
                              style='Sub.TLabel', wraplength=580)
        help_note.grid(row=5, column=0, columnspan=3, sticky='ew')
        self.connection_action = ttk.Button(top, text='接続の確認・修復…', command=self.reconnect_auto_watch)
        self.connection_action.grid(row=6, column=0, columnspan=3, sticky='w')
        self.connection_action.grid_remove()
        self.wrap_labels.extend((plan, updated, automatic, help_note))
        self.connection_note = tk.StringVar()
        self.update_connection_note()
        note = ttk.Label(top, textvariable=self.connection_note, style='Sub.TLabel', wraplength=580, justify='left')
        note.grid(row=7, column=0, columnspan=3, sticky='ew', pady=(4, 0))
        self.wrap_labels.append(note)

        order_bar = ttk.Frame(top, style='Main.TFrame')
        order_bar.grid(row=8, column=0, columnspan=3, sticky='ew', pady=(6, 0))
        ttk.Label(order_bar, text='並び順', style='Sub.TLabel').pack(side='left', padx=(0, 6))
        self.sort_choice = tk.StringVar(value='実行・再開待ちを優先')
        order = ttk.Combobox(order_bar, state='readonly', width=24,
                             textvariable=self.sort_choice, values=tuple(self.sort_options()))
        order.pack(side='left')
        order.bind('<<ComboboxSelected>>', self.apply_sort_choice)
        ttk.Button(order_bar, text='実行・監視中を上に戻す', command=self.prioritize_active).pack(side='left', padx=8)
        ttk.Label(order_bar, text='列名のクリックでも変更できます', style='Sub.TLabel').pack(side='left', padx=8)

        bar = ttk.Frame(top, style='Main.TFrame', padding=(0, 4, 0, 4))
        bar.grid(row=9, column=0, columnspan=3, sticky='ew')
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text='検索', style='Sub.TLabel').grid(row=0, column=0, padx=(0, 6))
        ttk.Entry(bar, textvariable=self.search, width=12).grid(row=0, column=1, sticky='ew', padx=(0, 8))
        ttk.Combobox(bar, state='readonly', width=11, values=['すべて', '実行・監視中', '利用枠停止', '要確認', '完了'],
                     textvariable=self.filter).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(bar, text='一覧・利用枠を更新', command=self.refresh_all).grid(row=0, column=3)

        views = ttk.Frame(body, style='Main.TFrame', padding=(16, 2))
        views.grid(row=1, column=0, sticky='ew')
        self.view_filter = tk.StringVar(value='すべて')
        for index, label in enumerate(('すべて', '解除待ち・再開中', '確認が必要')):
            ttk.Radiobutton(views, text=label, value=label, variable=self.view_filter,
                            command=self.render_rows).grid(row=0, column=index, padx=(0, 10))

        ttk.Button(views, text='詳細表示（プロジェクト・並べ替え）', command=self.toggle_details).grid(
            row=0, column=3, sticky='e')
        scope_note = ttk.Label(views, text='すべて：現在検出済みの通常チャットです。全履歴ではありません。',
                               style='Sub.TLabel', wraplength=580)
        scope_note.grid(row=1, column=0, columnspan=4, sticky='ew')
        self.wrap_labels.append(scope_note)

        table = ttk.Frame(body, style='Panel.TFrame', padding=1)
        table.grid(row=2, column=0, sticky='nsew', padx=16)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ('title', 'project', 'status', 'auto', 'reset')
        self.tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='browse', height=4)
        for col, label, width, anchor, stretch in [
            ('title', 'チャット名', 260, 'w', True),
            ('project', 'プロジェクト', 130, 'w', True),
            ('status', '状態', 150, 'center', False),
            ('auto', '再開対象', 90, 'center', False),
            ('reset', '利用枠の回復予定', 140, 'center', False),
        ]:
            self.tree.heading(col, text=label, command=lambda column=col: self.sort_rows(column))
            self.tree.column(col, width=width, minwidth=85, anchor=anchor, stretch=stretch)
        self.tree.tag_configure('limited', background='#FFF1E8')
        self.tree.tag_configure('blocked', background='#FCEDEE')
        self.tree.tag_configure('completed', foreground='#7A8798')
        vertical = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<<TreeviewSelect>>', self.select_row)
        self.tree.bind('<Double-1>', self.open_selected_chat)
        if getattr(self, 'simple_mode', True):
            self.tree.configure(displaycolumns=('title', 'auto', 'status', 'reset'))

        detail = ttk.Label(body, textvariable=self.details, style='Sub.TLabel', wraplength=580, justify='left')
        detail.grid(row=3, column=0, sticky='ew', padx=16, pady=4)
        self.wrap_labels.append(detail)
        target_panel = ttk.Frame(body, style='Main.TFrame', padding=(16, 0, 16, 2))
        target_panel.grid(row=4, column=0, sticky='ew')
        target_panel.columnconfigure(0, weight=1)
        self.target_included = tk.BooleanVar(value=False)
        self.target_reason = tk.StringVar(value='チャットを選ぶと対象を確認できます')
        self.target_toggle = ttk.Checkbutton(target_panel, variable=self.target_included,
                    text='このチャットを自動再開の対象にする', command=self.change_target)
        self.target_toggle.grid(row=0, column=0, sticky='w')
        self.target_toggle.state(['disabled'])
        target_note = ttk.Label(target_panel, textvariable=self.target_reason,
                                style='Sub.TLabel', wraplength=580)
        target_note.grid(row=1, column=0, columnspan=3, sticky='ew')
        self.wrap_labels.append(target_note)
        selected_actions = ttk.Menubutton(target_panel, text='選択チャットの操作')
        selected_actions.grid(row=0, column=1, sticky='e', padx=(8, 0))
        selected_menu = tk.Menu(selected_actions, tearoff=False)
        selected_menu.add_command(label='選択したチャットを対象にする', command=lambda: self.toggle(True))
        selected_menu.add_command(label='完了として記録（監視を終了）', command=lambda: self.finish('completed'))
        selected_menu.add_command(label='要確認として記録（自動再開を停止）', command=lambda: self.finish('blocked'))
        selected_menu.add_command(label='確認して選び直す（試行履歴は保持）', command=self.review_reset)
        selected_menu.add_command(label='詳細情報（ID・場所）', command=self.show_technical_details)
        selected_actions.configure(menu=selected_menu)
        self.navigation_button = ttk.Button(target_panel, text='ChatGPTで開く', command=self.open_selected_chat)
        self.navigation_button.grid(row=0, column=2, padx=(8, 0), sticky='e')
        footer = ttk.Frame(body, style='Main.TFrame', padding=(16, 0, 16, 4))
        footer.grid(row=6, column=0, sticky='ew')
        footer.columnconfigure(0, weight=1)
        for row, variable in enumerate((self.status, self.tray_state)):
            label = ttk.Label(footer, textvariable=variable, style='Sub.TLabel', wraplength=580, justify='left')
            label.grid(row=row, column=0, sticky='ew')
            self.wrap_labels.append(label)
        self.log = tk.Text(body, height=1, wrap='word', relief='flat', background='#EAF0F6',
                           foreground='#344A61', font=('TkDefaultFont', 9))
        self.log.grid(row=7, column=0, sticky='ew', padx=16, pady=(0, 6))
        self.log.configure(state='disabled')
        self.advanced_widgets = (bar, self.log, note, order_bar, selected_actions)
        if getattr(self, 'simple_mode', True):
            for widget in self.advanced_widgets:
                widget.grid_remove()
        self.refresh_auto_status()
        self.render_usage()
        for label in self.wrap_labels:
            label.bind('<Configure>', self.resize_label_text, add='+')

    def resize_label_text(self, event):
        # Use allocated label geometry, not transient native-window Configure sizes.
        if event.widget in self.wrap_labels and event.width > 1:
            width = max(1, event.width - 4)
            if str(event.widget.cget('wraplength')) != str(width):
                event.widget.configure(wraplength=width)

    @staticmethod
    def sort_options():
        options = {'実行・再開待ちを優先': (None, False)}
        for column, name in (('title', 'チャット名'), ('project', 'プロジェクト'),
                             ('status', '状態'), ('auto', 'チャット別の自動再開'), ('reset', '回復予定')):
            options[name + '：昇順'] = (column, False)
            options[name + '：降順'] = (column, True)
        return options

    def apply_sort_choice(self, _event=None):
        choice = self.sort_options().get(self.sort_choice.get())
        if choice is not None:
            self.sort_column, self.sort_descending = choice
            self.render_rows()

    def update_sort_choice(self):
        if hasattr(self, 'sort_choice'):
            state = (getattr(self, 'sort_column', None), getattr(self, 'sort_descending', False))
            for label, choice in self.sort_options().items():
                if choice == state:
                    self.sort_choice.set(label)
                    break

    def show_usage_details(self):
        view = build_usage_view(getattr(self, 'last_usage_result', None),
                                failed=getattr(self, 'usage_failed', False))
        messagebox.showinfo('利用枠の詳細', usage_view_text(view), parent=self.root)

    def write(self, s):
        self.log.configure(state='normal')
        self.log.insert('end', str(s) + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def clear_usage_display(self, config):
        self.last_usage_result = None
        self.usage_failed = False
        self.desktop_read_state = None
        if hasattr(self, 'connection_note'):
            self.connection_note.set(connection_text(config))
        if hasattr(self, 'plan_note'):
            self.plan_note.set('プラン: 未確認')
        self.render_usage()

    def render_connection_reading(self, config):
        """読取りの失敗・期限切れを接続説明とプランにも反映する。外部へは接続しない。"""
        reading = getattr(self, 'last_usage_result', None)
        failed = getattr(self, 'usage_failed', False)
        historical = build_usage_view(reading, failed=failed)['stale'] or failed
        if historical:
            stale = dict(reading, desktop_connection='unavailable', account_match=None) if reading else None
            note = ('最新の読取りは失敗しました。' if failed else '取得から時間が経過しました。')
            note += 'プラン情報は前回取得値です。' if reading else '接続・プランは未確認です。'
            if hasattr(self, 'connection_note'):
                self.connection_note.set(note + '\n' + connection_text(config, stale))
            self.desktop_read_state = None
            source = ((reading.get('peer_limits') or {})
                      if reading and reading.get('desktop_connection') == 'read' else reading or {})
            if hasattr(self, 'plan_note'):
                plan = source.get('plan_type')
                label = PLAN_LABELS.get(plan, '未確認') if isinstance(plan, str) else '未確認'
                self.plan_note.set('プラン（前回取得・更新待ち）: ' + label if reading else 'プラン: 未確認')
            return
        if hasattr(self, 'connection_note'):
            self.connection_note.set(connection_text(config, reading))
        self.desktop_read_state = (reading or {}).get('desktop_connection')
        source = ((reading.get('peer_limits') or {})
                  if reading and self.desktop_read_state == 'read' else reading or {})
        value = source.get('plan_type')
        if hasattr(self, 'plan_note'):
            self.plan_note.set('プラン: ' + (PLAN_LABELS.get(value, '未確認')
                                           if isinstance(value, str) else '未確認'))

    def update_connection_note(self, reading=None, *, historical=False, config=None):
        config = connection_options(self.db) if config is None else config
        self.refresh_auto_status(config=config)
        if historical:
            self.usage_failed = True
            self.render_connection_reading(config)
            return
        if reading is None:
            self.clear_usage_display(config)
            self.usage_status.set(usage_unknown_text('次の読取りで確認します（取得元は公式CLIのログイン先です）'))
        else:
            self.last_usage_result = reading
            self.usage_failed = False
            self.render_connection_reading(config)

    def setup_connection(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('接続設定')
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='公式Desktopと同じ共有サーバーへ接続します。\n'
                  '共有サーバーは、この画面から自動起動しません。',
                  wraplength=520, justify='left').pack(anchor='w', pady=(0, 12))
        previous = connection_options(self.db).get('executable')
        selected = [previous] if isinstance(previous, str) and previous else []
        file_note = tk.StringVar(value='CLIを選択済み' if selected else '公式CLIを選択してください')
        def choose():
            value = filedialog.askopenfilename(parent=dialog, title='公式Codex CLIを選択')
            if value:
                selected[:] = [value]
                file_note.set('CLIを選択済み')
        ttk.Button(frame, text='公式CLIを選択', command=choose).pack(anchor='w')
        ttk.Label(frame, textvariable=file_note).pack(anchor='w', pady=(4, 12))
        def prepare():
            try:
                if not selected:
                    raise ValueError()
                prepare_connection(self.db, selected[0])
            except Exception:
                messagebox.showerror('準備できません', '公式CLIと配置先の権限を確認してください。', parent=dialog)
                return
            self.update_connection_note()
            self.refresh()
            self.write('接続の準備を保存しました。Desktop側を接続してから、接続先と操作ルールを確認してください。')
        ttk.Button(frame, text='接続の準備を保存（送信は無効）', command=prepare).pack(anchor='w', pady=(0, 12))
        desktop = tk.BooleanVar(value=False)
        manual = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, variable=desktop,
                        text='公式Desktopが同じ共有サーバーを使用する接続設定を確認しました').pack(anchor='w')
        ttk.Checkbutton(frame, variable=manual,
                        text='対象チャットを手動操作するときは、先に監視を停止します').pack(anchor='w', pady=(6, 0))
        ttk.Label(frame, text='完全に同時の手動操作との競合は排除できません。\n'
                  '接続設定を保存すると、監視対象はすべて解除されます。',
                  wraplength=520, justify='left').pack(anchor='w', pady=12)
        def save():
            try:
                if not selected:
                    raise ValueError()
                configure_connection(self.db, selected[0], desktop_confirmed=desktop.get(),
                                     manual_exclusion_acknowledged=manual.get())
            except Exception:
                messagebox.showerror('設定できません', '公式CLIと両方の確認項目、ファイルの権限を確認してください。', parent=dialog)
                return
            self.update_connection_note()
            self.refresh()
            self.write('接続設定を保存しました。接続状態を確認してから監視対象を選び直してください。')
            dialog.destroy()
        ttk.Button(frame, text='確認して保存', command=save).pack(anchor='e')

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def require_selected(self):
        tid = self.selected()
        if not tid:
            messagebox.showinfo('チャットを選択してください',
                                '一覧から操作するチャットの行を選んでください。', parent=self.root)
        return tid

    def refresh(self):
        selected = self.selected()
        previous = {item['id']: item.get('excluded_reason') for item in self.known}
        saved_ids = [row['id'] for row in list_threads(self.db)]
        self.known, warning = discover(include_ids=saved_ids)
        current = {item['id']: item.get('excluded_reason') for item in self.known}
        changed = {tid for tid in previous.keys() | current.keys()
                   if tid not in previous or tid not in current or previous[tid] != current[tid]}
        if changed:
            self.invalidate_monitor_results(changed)
        if warning: self.write('読取注意: ' + warning)
        if self.known: sync(self.db, self.known)
        self.reload_rows()
        self.render_rows(selected)

    @staticmethod
    def is_visible_row(row, live):
        """検出済みで除外理由のない、一覧に表示できる行かを判定する。"""
        observed = live.get(row['id'])
        return observed is not None and not observed.get('excluded_reason')

    def render_rows(self, selected=None, *, config=None):
        selected = selected or self.selected()
        for item in self.tree.get_children(): self.tree.delete(item)
        live = {x['id']: x for x in self.known}
        simple = getattr(self, 'simple_mode', False)
        query = '' if simple else self.search.get().casefold().strip()
        view = self.view_filter.get() if hasattr(self, 'view_filter') else 'すべて'
        mode = 'すべて' if simple else self.filter.get()
        config = connection_options(self.db) if config is None else config
        self.refresh_auto_status(config=config)
        configured = display_connection_ready(config)
        count = 0
        for row in self.ordered_rows(live):
            if not self.is_visible_row(row, live):
                continue
            t = live[row['id']]
            if view == '解除待ち・再開中' and row['state'] not in ('waiting', 'claimed', 'dispatched', 'monitoring'):
                continue
            if view == '確認が必要' and row['state'] != 'blocked':
                continue
            title = row['title'] or '(タイトルなし)'
            project = pathlib.Path(row['cwd']).name if row['cwd'] else '—'
            if query and query not in (title + ' ' + project + ' ' + row['id']).casefold(): continue
            if mode == '有効のみ' and not row['enabled']: continue
            if mode == '実行・監視中' and not self.is_active(row, t): continue
            if mode == '利用枠停止' and not t.get('limited'): continue
            if mode == '要確認' and row['state'] != 'blocked': continue
            if mode == '完了' and row['state'] != 'completed': continue
            status = self.row_status(row, t)
            reset = self.reset_text(row, t, configured)
            tags = ('limited',) if t.get('limited') else (row['state'],)
            self.tree.insert('', 'end', iid=row['id'], values=(title, project, status, self.target_text(row), reset), tags=tags)
            count += 1
        if selected and self.tree.exists(selected): self.tree.selection_set(selected)
        self.select_row()
        self.update_status(count, configured=configured)

    @staticmethod
    def is_active(row, observed):
        return (observed.get('turn_status') == 'inProgress' or
                bool(row['enabled']) and row['state'] not in ('blocked', 'completed'))

    @staticmethod
    def row_status(row, observed):
        labels = {'waiting': '制限解除待ち', 'claimed': '再開確認中', 'completed': '完了',
                  'blocked': '要確認', 'dispatched': '再開要求済み', 'monitoring': '再開後を監視中'}
        if row['state'] in labels:
            return labels[row['state']]
        if observed.get('turn_status') == 'inProgress':
            return '実行中（読取候補）'
        if observed.get('limited'):
            return '利用枠停止の候補'
        return '再開処理なし'

    @staticmethod
    def reset_text(row, observed, configured):
        if row['state'] in ('blocked', 'completed'):
            return '対象外'
        if row['reset_at'] is not None:
            try:
                return datetime.datetime.fromtimestamp(row['reset_at']).astimezone().strftime('%m/%d %H:%M')
            except (ValueError, OverflowError, OSError, TypeError):
                return '日時を確認できません'
        if not observed.get('limited') and row['state'] != 'waiting':
            return '制限停止なし'
        if not configured:
            return '接続確認が必要'
        return '日時を取得待ち' if row['enabled'] else '自動再開OFF'

    def sort_rows(self, column):
        if column not in ('title', 'project', 'status', 'auto', 'reset'):
            return
        self.sort_descending = (not getattr(self, 'sort_descending', False)
                                if getattr(self, 'sort_column', None) == column else False)
        self.sort_column = column
        self.update_sort_choice()
        self.render_rows()

    def prioritize_active(self):
        self.sort_column = None
        self.sort_descending = False
        self.update_sort_choice()
        self.render_rows()

    def ordered_rows(self, live):
        column = getattr(self, 'sort_column', None)
        descending = getattr(self, 'sort_descending', False)
        labels = {'title': 'チャット名', 'project': 'プロジェクト', 'status': '状態',
                  'auto': '再開対象', 'reset': '利用枠の回復予定'}
        for name, label in labels.items():
            arrow = (' ▼' if descending else ' ▲') if name == column else ''
            self.tree.heading(name, text=label + arrow)
        groups = [[], [], []] if column is None else [[]]
        for row in self.rows:
            observed = live.get(row['id'], {})
            priority = (0 if self.is_active(row, observed) else
                        1 if observed.get('limited') and row['state'] not in ('blocked', 'completed') else 2)
            groups[priority if column is None else 0].append(row)
        def natural(value):
            return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                         for part in re.split(r'(\d+)', value))
        def key(row):
            if column == 'reset': return row['reset_at']
            if column == 'auto': return int(not bool(row['auto_excluded']))
            if column == 'status': return self.row_status(row, live.get(row['id'], {}))
            if column == 'project': return natural(pathlib.Path(row['cwd']).name)
            return natural(row['title'] or '')
        ordered = []
        for group in groups:
            # Missing dates stay last in either direction; never sort formatted dates.
            present = [row for row in group if column != 'reset' or row['reset_at'] is not None]
            missing = [row for row in group if column == 'reset' and row['reset_at'] is None]
            ordered.extend(sorted(present, key=key, reverse=descending))
            ordered.extend(missing)
        return ordered

    def confirm_limited_targets(self, candidates):
        dialog = tk.Toplevel(self.root)
        dialog.title('利用枠停止の対象を選択')
        dialog.transient(self.root)
        self.configure_window(dialog, max_width=1000, max_height=500)
        dialog.resizable(True, True)
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill='both', expand=True)
        additive_key = '⌘' if sys.platform == 'darwin' else 'Ctrl'
        ttk.Label(frame, text=f'検索・表示フィルター外も含む全停止候補 {len(candidates)}件です。必要な行を選択してください。'
                  f'\n{additive_key}を押しながらクリックで追加・解除、Shiftで範囲選択できます。'
                  '\n今は送信せず、利用枠の回復と安全条件を確認してから再開します。'
                  '\n監視中のチャットを手動操作する前に、自動再開をOFFにしてください。',
                  wraplength=max(1, min(660, dialog.winfo_screenwidth() - 80))).pack(anchor='w', pady=(0, 10))
        table = ttk.Frame(frame)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        columns = ('title', 'project', 'id')
        tree = ttk.Treeview(table, columns=columns, show='headings', selectmode='extended')
        vertical = ttk.Scrollbar(table, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(table, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        values = [(row['title'] or '(タイトルなし)',
                   pathlib.Path(row['cwd']).name if row['cwd'] else '—', row['id'])
                  for row in candidates]
        font = tkfont.nametofont('TkDefaultFont', root=dialog)
        for index, (column, heading) in enumerate(zip(columns, ('チャット名', 'プロジェクト', '識別用ID'))):
            tree.heading(column, text=heading)
            width = max(font.measure(heading), *(font.measure(row[index]) for row in values)) + 30
            tree.column(column, width=max(120, width), stretch=False)
        for row in values:
            tree.insert('', 'end', iid=row[2], values=row)
        result = []
        count = tk.StringVar(master=dialog, value=f'選択 0件 / 全{len(candidates)}件')
        count_label = ttk.Label(frame, textvariable=count)
        actions = ttk.Frame(frame)
        # 操作と件数の領域を先に確保し、残りをスクロール一覧に使う。
        actions.pack(side='bottom', fill='x')
        count_label.pack(side='bottom', anchor='w', pady=(8, 4))
        table.pack(fill='both', expand=True)

        def update_count(event=None):
            selected_count = len(tree.selection())
            count.set(f'選択 {selected_count}件 / 全{len(candidates)}件')
            accept_button.configure(state='normal' if selected_count else 'disabled')

        def select_all():
            tree.selection_set(tree.get_children())
            update_count()

        def clear_all():
            tree.selection_remove(tree.selection())
            update_count()

        def cancel(event=None):
            dialog.destroy()

        def accept():
            selected = list(tree.selection())
            if selected:
                result.extend(selected)
                dialog.destroy()

        ttk.Button(actions, text='全件選択', command=select_all).pack(side='left')
        ttk.Button(actions, text='全解除', command=clear_all).pack(side='left', padx=(8, 0))
        accept_button = ttk.Button(actions, text='選択した対象をON', command=accept, state='disabled')
        accept_button.pack(side='right')
        ttk.Button(actions, text='取消し', command=cancel).pack(side='right', padx=(0, 8))
        tree.bind('<<TreeviewSelect>>', update_count)
        dialog.bind('<Escape>', cancel)
        dialog.protocol('WM_DELETE_WINDOW', cancel)
        dialog.grab_set()
        tree.focus_set()
        dialog.wait_window()
        return result

    def enable_limited(self):
        # 一括選択の互換処理は検証用に保持。画面では自動検出と個別の対象指定を使う。
        self.refresh()
        live = {item['id']: item for item in self.known}
        candidates = [row for row in self.rows if not row['enabled'] and row['state'] in ('idle', 'waiting')
                      and live.get(row['id'], {}).get('limited') is True
                      and live[row['id']].get('failure_turn')]
        if not candidates:
            messagebox.showinfo('停止候補なし', '新しく自動再開の対象にできる利用枠停止はありません。', parent=self.root)
            return
        selection = self.confirm_limited_targets(candidates)
        if not selection:
            return
        try:
            candidate_ids = {row['id'] for row in candidates}
            if (not isinstance(selection, (list, tuple)) or
                    any(not isinstance(tid, str) for tid in selection) or
                    len(set(selection)) != len(selection) or not set(selection) <= candidate_ids):
                raise ValueError('対象の選択が不正です')
            selected_ids = set(selection)
            candidates = [row for row in candidates if row['id'] in selected_ids]
            current, warning = discover(include_ids=[row['id'] for row in candidates])
            observed = {item['id']: item for item in current}
            if warning or any(not observed.get(row['id'], {}).get('limited') or
                              observed[row['id']].get('failure_turn') != live[row['id']]['failure_turn']
                              for row in candidates):
                raise ValueError('停止候補が変わりました')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for row in candidates:
                    now = self.db.execute('SELECT enabled,state,generation FROM threads WHERE id=?', (row['id'],)).fetchone()
                    if not now or tuple(now) != (row['enabled'], row['state'], row['generation']):
                        raise ValueError('対象の状態が変わりました')
                    enable(self.db, row['id'], True)
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        except (ValueError, RuntimeError, sqlite3.Error):
            messagebox.showwarning('設定を見送りました', '停止候補または状態が変わりました。一覧を確認してやり直してください。', parent=self.root)
            self.refresh()
            return
        self.write(f'{len(candidates)}件を自動再開の対象にしました。利用枠と安全条件を確認します。')
        self.invalidate_monitor_results(selected_ids)
        self.refresh()

    def update_status(self, count, *, configured=None):
        feedback = getattr(self, 'navigation_feedback', '')
        suffix = '\n' + feedback if feedback else ''
        if getattr(self, 'simple_mode', False):
            self.status.set(f'表示 {count}件  |  全体OFFでも個別の対象指定は保持します' + suffix)
            return
        if configured is None:
            configured = display_connection_ready(connection_options(self.db))
        visible_ids = {item['id'] for item in self.known if not item.get('excluded_reason')}
        enabled = sum(bool(row['enabled']) for row in self.rows if row['id'] in visible_ids)
        mode = (f'監視対象 {enabled}件（接続結果は監視ログに表示）' if configured else
                '自動再開: 接続未設定のため送信停止')
        order = {'title': 'チャット名', 'project': 'プロジェクト', 'status': '状態', 'auto': '自動再開', 'reset': '回復予定'}.get(getattr(self, 'sort_column', None), '実行・監視優先')
        self.status.set(f'検出 {len(visible_ids)} 件 / 表示 {count} 件  |  並び順: {order}  |  {mode}' + suffix)

    @staticmethod
    def target_text(row):
        if row['state'] == 'blocked':
            return '要確認'
        if row['state'] == 'completed':
            return '完了'
        return '除外' if row['auto_excluded'] else '対象'

    def change_target(self):
        tid = self.selected()
        if not tid:
            return
        try:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT state FROM threads WHERE id=?', (tid,)).fetchone()
            if row and row[0] not in ('blocked', 'completed', 'claimed', 'dispatched'):
                enable(self.db, tid, bool(self.target_included.get()))
            self.db.execute('COMMIT')
            self.invalidate_monitor_results({tid})
        except (ValueError, RuntimeError, sqlite3.Error):
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            messagebox.showwarning('変更できません', '状態が変わりました。対象を確認してください。', parent=self.root)
        self.reload_rows()
        self.render_rows(tid)

    def update_target_controls(self, row):
        if not hasattr(self, 'target_toggle'):
            return
        editable = row is not None and row['state'] not in ('blocked', 'completed', 'claimed', 'dispatched')
        self.target_included.set(bool(row and not row['auto_excluded']))
        self.target_toggle.state(['!disabled'] if editable else ['disabled'])
        reason = ('対象指定を保持します。全体OFFの間は送信しません。' if editable else
                  '完了・要確認・送信処理中は、この操作で再有効化できません。' if row else
                  'チャットを選ぶと対象を確認できます')
        self.target_reason.set(reason)

    def select_row(self, _event=None):
        tid = self.selected()
        row = next((r for r in self.rows if r['id'] == tid), None)
        self.update_target_controls(row)
        if not row:
            self.details.set('利用枠で止まったチャットを自動検出します。行を選ぶと対象を確認できます。'
                             if getattr(self, 'simple_mode', False) else
                             'チャットを選び、下の対象チェックで個別に除外・復帰できます。')
            return
        title = row['title'] or '(タイトルなし)'
        project = pathlib.Path(row['cwd']).name if row['cwd'] else 'プロジェクトなし'
        title = title[:59] + '…' if len(title) > 60 else title
        project = project[:23] + '…' if len(project) > 24 else project
        self.details.set(f'選択: {title}  |  {project}')

    def open_selected_chat(self, event=None):
        thread = getattr(self, 'navigation_thread', None)
        if (self.closing or getattr(self, 'navigation_pending', False)
                or thread is not None and thread.is_alive()):
            return 'break' if event is not None else None
        if event is not None:
            if self.tree.identify_region(event.x, event.y) not in ('cell', 'tree'):
                return 'break'
            tid = self.tree.identify_row(event.y)
            if not tid or not any(row['id'] == tid for row in self.rows):
                return 'break'
            self.tree.selection_set(tid)
            self.select_row()
        else:
            tid = self.selected()
            if not tid:
                self.navigation_feedback = 'ChatGPTで開くチャットを一覧から選んでください。'
                self.status.set(self.navigation_feedback)
                return
        self.navigation_cancel = threading.Event()
        self.navigation_results = queue.Queue(maxsize=1)
        self.navigation_started = time.monotonic()
        deadline = self.navigation_started + 3 if sys.platform == 'darwin' else None
        self.navigation_deadline = deadline
        self.navigation_timed_out = False
        generation = self.navigation_generation = getattr(self, 'navigation_generation', 0) + 1
        self.navigation_pending = True
        self.navigation_button.configure(state='disabled')
        self.navigation_feedback = 'ChatGPTへチャットの表示を要求しています。'
        self.status.set(self.navigation_feedback)
        cancelled, results = self.navigation_cancel, self.navigation_results

        def worker():
            outcome = 'failed'
            try:
                def before_open():
                    return not cancelled.is_set() and not self.closing
                if before_open():
                    result = request_chat_open(tid, before_open=before_open, deadline=deadline)
                    outcomes = ('requested', 'unavailable', 'timeout', 'cancelled', 'failed')
                    outcome = ('requested' if result is True else result
                               if isinstance(result, str) and result in outcomes else 'failed')
                    if not before_open():
                        outcome = 'cancelled'
                else:
                    outcome = 'cancelled'
            except Exception:
                # Expected navigation errors and unexpected worker failures share a
                # fixed user message; private paths and diagnostics stay out of Tk.
                outcome = 'cancelled' if cancelled.is_set() else 'failed'
            finally:
                results.put_nowait(outcome)

        self.navigation_thread = threading.Thread(target=worker, daemon=False, name='thread-navigation')
        try:
            self.navigation_thread.start()
        except Exception:
            self.navigation_results.put_nowait('failed')
        self.root.after(100, lambda: self.drain_navigation(generation))
        return 'break' if event is not None else None

    def drain_navigation(self, generation=None):
        current = getattr(self, 'navigation_generation', 0)
        if generation is not None and generation != current:
            return
        if not getattr(self, 'navigation_pending', False):
            return
        if self.closing:
            self.navigation_cancel.set()
            return
        if self.navigation_thread.is_alive():
            deadline = getattr(self, 'navigation_deadline', None)
            if deadline is not None and time.monotonic() >= deadline:
                self.navigation_timed_out = True
                self.navigation_cancel.set()
                self.navigation_feedback = '表示要求の確認時間を超えました。補助処理を終了しています。'
                self.status.set(self.navigation_feedback)
            self.root.after(100, lambda: self.drain_navigation(current))
            return
        try:
            outcome = self.navigation_results.get_nowait()
        except queue.Empty:
            outcome = 'failed'
        if getattr(self, 'navigation_timed_out', False):
            outcome = 'timeout'
        self.navigation_pending = False
        self.navigation_button.configure(state='normal')
        messages = {
            'requested': 'ChatGPTに元のチャットの表示を要求しました。実際の表示先を確認してください。',
            'unavailable': 'ChatGPTを利用できません。アプリの配置を確認してください。',
            'timeout': '表示要求の確認時間を超えました。ChatGPTの表示先を確認してください。自動再試行はしません。',
            'cancelled': 'チャットの表示要求を取り消しました。',
            'failed': '表示要求の結果を確認できません。ChatGPT側で対象を確認してください。',
        }
        message = messages.get(outcome, messages['failed'])
        elapsed = max(0, time.monotonic() - getattr(self, 'navigation_started', time.monotonic()))
        message += f'（処理 {elapsed:.1f}秒）'
        self.navigation_feedback = message
        self.status.set(message)
        if outcome != 'cancelled':
            self.write(message)

    def show_technical_details(self):
        tid = self.require_selected()
        if not tid:
            return
        row = next((r for r in self.rows if r['id'] == tid), None)
        if row:
            messagebox.showinfo('選択したチャットの詳細',
                                f"{row['title']}\nプロジェクト: {row['cwd'] or '—'}\nスレッドID: {tid}",
                                parent=self.root)

    def toggle_selected(self, _event=None):
        tid = self.selected()
        row = next((r for r in self.rows if r['id'] == tid), None)
        if row: self.toggle(not bool(row['enabled']))

    def toggle(self, value):
        tid = self.require_selected()
        if not tid: return
        try:
            enable(self.db, tid, value)
            self.write(('自動再開をONにしました。利用枠と安全条件を確認します。' if value else '自動再開をOFFにしました。'))
        except (ValueError, RuntimeError, sqlite3.Error):
            messagebox.showwarning('変更できません', '現在の状態では変更できません。チャットの状態を確認してください。')
        self.refresh()

    def review_reset(self):
        tid = self.require_selected()
        if not tid: return
        try:
            row = self.db.execute('SELECT state,enabled,generation FROM threads WHERE id=?', (tid,)).fetchone()
            if not row or row[0] not in ('blocked', 'completed') or row[1]:
                raise ValueError()
        except (ValueError, RuntimeError, sqlite3.Error):
            messagebox.showwarning('選び直せません',
                                   '無効な「要確認」または「完了」のチャットだけ選び直せます。',
                                   parent=self.root)
            return
        if not messagebox.askyesno(
                '監視対象の選び直し',
                '公式Desktopで、このチャットの実行・承認待ちが終了していることと、'
                '現在の接続先を確認しましたか？\n\n'
                '監視は自動では有効になりません。確認後に監視対象を選択してください。\n'
                '試行履歴は保持し、同じ停止ターンへの再送は行いません。\n\n'
                '確認したうえで、選び直せる状態に戻しますか？',
                parent=self.root, default='no'):
            return
        try:
            # 確認した世代と操作時の状態条件を、markのSQLで照合する。
            mark(self.db, tid, 'idle', expected_generation=row[2])
        except (ValueError, RuntimeError, sqlite3.Error):
            messagebox.showwarning('選び直せません',
                                   '現在の状態では選び直せません。チャットの状態を確認してください。',
                                   parent=self.root)
        else:
            self.invalidate_monitor_results({tid})
            self.write('選び直せる状態に戻しました。監視は無効のままです。必要な対象を選択してください。')
        self.refresh()

    def finish(self, status):
        tid = self.require_selected()
        if not tid:
            return
        if messagebox.askyesno('確認', 'このチャットを「' + ('完了' if status == 'completed' else '要確認') + '」として記録しますか？',
                               parent=self.root, default='no'):
            mark(self.db, tid, status)
            self.invalidate_monitor_results({tid})
            self.refresh()

    def preview(self):
        # 送信なしの互換処理として保持。画面の候補確認は一覧と対象チェックで行う。
        items = dry_run(self.db, self.known)
        self.write(f'再開予定の候補（送信なし）: {len(items)}件')

    def periodic(self):
        if self.closing: return
        try:
            self.refresh()
        except Exception:
            self.write('一覧を更新できません。次回再確認します')
        finally:
            if not self.closing:
                self.root.after(30000, self.periodic)

    def start_tray(self):
        self.tray_is_mac = sys.platform == 'darwin'
        self.tray_actions = queue.SimpleQueue()
        deps = tray_dependencies()
        if not deps:
            self.tray_state.set(f'{self.tray_location()}：追加ライブラリが必要')
            self.write('常駐アイコンを開始できません。画面を表示したまま使えます。')
            return
        pystray, Image, ImageDraw = deps
        self.window_hidden = self.root.state() in ('withdrawn', 'iconic')
        def callback(func):
            # Native AppKit callbacks must not enter Tcl, even via after().
            # Windows callbacks can also arrive on the tray worker thread.
            return lambda icon, item: self.tray_actions.put(func)
        menu = pystray.Menu(
            # Native text callbacks may run outside Tk's event loop.
            pystray.MenuItem(lambda item: 'ウィンドウを開く' if self.window_hidden else 'トレイにしまう',
                             callback(self.toggle_tray_window), default=True),
            pystray.MenuItem('一覧を更新', callback(self.refresh)),
            pystray.MenuItem('終了', callback(self.quit_app)),
        )
        try:
            self.tray = pystray.Icon('codex_safe_resume', create_tray_image(Image, ImageDraw), 'Codex 自動再開管理', menu)
            self.tray_state.set(f'{self.tray_location()}：準備中')
            self.tray_pending = False
            if sys.platform == 'darwin':
                # Darwinはdetachedのreadyが同期的。既定setup threadに
                # AppKit表示を任せず、Tkのメインスレッドで表示する。
                self.tray.run_detached(setup=lambda icon: None)
                self.tray.visible = True
            else:
                # 公開runを所有daemon threadで実行する。既定setup threadも
                # daemonを継承し、native準備失敗時にプロセス終了を妨げない。
                icon = self.tray
                exited = threading.Event()
                self.tray_exited = exited
                def run_tray():
                    try:
                        icon.run()
                    except Exception:
                        # 本文を出力せず、Tk側の準備確認で失敗を表示する。
                        pass
                    finally:
                        # backend終了は通知だけ。Tk操作はメインループが行う。
                        exited.set()
                self.tray_thread = threading.Thread(target=run_tray, daemon=True,
                                                    name='codex-tray')
                self.tray_thread.start()
            self.check_tray_ready()
            if self.tray is not None:
                self.root.after(100, self.drain_tray_actions)
        except Exception:
            self.tray_failed()

    def drain_tray_actions(self):
        if self.closing:
            return
        try:
            # Bound each pass so a burst cannot starve the Tk event loop.
            for _ in range(32):
                try:
                    action = self.tray_actions.get_nowait()
                except queue.Empty:
                    break
                action()
                if self.closing:
                    break
        finally:
            if not self.closing and self.tray is not None:
                self.root.after(100, self.drain_tray_actions)

    def check_tray_ready(self, remaining=30):
        if self.closing or self.tray is None:
            return
        try:
            exited = getattr(self, 'tray_exited', None)
            if exited is not None and exited.is_set():
                self.tray_failed()
                return
            if getattr(self, 'tray_is_mac', False):
                native = mac_tray_snapshot(self.tray)
                if native != getattr(self, 'tray_native_state', None):
                    self.tray_native_state = native
                    self.write('常駐の配置診断: ' + json.dumps(native, ensure_ascii=False))
                if native['status'] != 'presented':
                    self.tray_available = False
                    if self.root.state() in ('withdrawn', 'iconic'):
                        self.show_window()
                    self.tray_state.set('メニューバー：表示位置を確認中（画面は表示したままです）')
                    self.root.after(100 if remaining else 5000,
                                    lambda: self.check_tray_ready(max(0, remaining - 1)))
                    return
            if self.tray.visible:
                if not self.tray_available:
                    self.tray_state.set(f'{self.tray_location()}：常駐中（×で画面を隠します）')
                self.tray_available = True
                self.tray_pending = False
                if exited is not None or getattr(self, 'tray_is_mac', False):
                    self.root.after(5000 if getattr(self, 'tray_is_mac', False) else 1000, self.check_tray_ready)
            elif remaining:
                self.root.after(100, lambda: self.check_tray_ready(remaining - 1))
            elif exited is not None:
                # 時間経過だけでは起動を取り消せない。所有handleを保持し、
                # 遅い準備の完了または実際のbackend終了を低頻度で確認する。
                self.tray_available = False
                if not getattr(self, 'tray_pending', False):
                    self.tray_pending = True
                    self.tray_state.set(f'{self.tray_location()}：起動を待っています（画面は表示したままです）')
                    self.show_window()
                self.root.after(1000, lambda: self.check_tray_ready(remaining=0))
            else:
                self.tray_failed()
        except Exception:
            self.tray_failed()

    def tray_failed(self):
        self.tray_available = False
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                # 終了を再試行できるよう、停止未確認のhandleは保持する。
                pass
            else:
                self.tray = None
        self.tray_state.set(f'{self.tray_location()}：利用不可')
        self.show_window()
        self.write('常駐アイコンを利用できません。画面を表示したままにします。')

    def sync_tray_window(self, event=None):
        if self.closing or (event is not None and event.widget is not self.root):
            return
        hidden = self.root.state() in ('withdrawn', 'iconic')
        if hidden == getattr(self, 'window_hidden', False):
            return
        self.window_hidden = hidden
        if self.tray is not None:
            try:
                self.tray.update_menu()
            except Exception:
                self.write('常駐メニューの表示を更新できません。')

    def toggle_tray_window(self):
        if self.closing:
            return
        if self.root.state() in ('withdrawn', 'iconic'):
            self.show_window()
        else:
            self.on_close()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.sync_tray_window()

    def on_close(self):
        if self.closing:
            return
        try:
            if getattr(self, 'tray_is_mac', False) and self.tray is not None:
                self.tray_available = mac_tray_snapshot(self.tray)['status'] == 'presented'
            exited = getattr(self, 'tray_exited', None)
            visible = (self.tray_available and self.tray is not None and self.tray.visible
                       and (exited is None or not exited.is_set()))
        except Exception:
            visible = False
        if visible:
            self.root.withdraw()
            self.sync_tray_window()
        elif self.tray_requested:
            self.tray_available = False
            self.show_window()
            self.write('常駐アイコンを確認できないため画面を残します。終了は「設定」から選べます。')
        else:
            self.quit_app()

    def quit_app(self):
        if self.closing: return
        self.closing = True
        usage_cancel = getattr(self, 'usage_cancel', None)
        if usage_cancel is not None:
            usage_cancel.set()
        cancelled = getattr(self, 'navigation_cancel', None)
        if cancelled is not None:
            cancelled.set()
        try:
            set_auto_watch(self.db, False)
        except Exception:
            self.hold_quit('停止設定を保存できません。アプリは終了していません。')
            return
        self.root.title('Codex Safe Resume（終了処理中）')
        for name in ('auto_status', 'status'):
            variable = getattr(self, name, None)
            if variable is not None:
                variable.set('終了処理中：自動再開はOFFです。処理結果を確認しています。')
        self.shutdown_started = False
        self.finish_quit()

    def hold_quit(self, message):
        self.closing = False
        self.root.title('Codex Safe Resume')
        self.show_window()
        self.write(message)
        if hasattr(self, 'auto_status'):
            self.auto_status.set('要確認：終了前の送信状態を確認してください')
        for method in ('monitor_tick', 'drain_monitor', 'usage_tick', 'drain_usage',
                       'periodic', 'display_tick', 'poll_desktop_connection', 'drain_tray_actions',
                       'drain_navigation'):
            self.root.after(1000, getattr(self, method))

    def finish_quit(self):
        for name in ('monitor_thread', 'usage_thread', 'queue_setup_thread', 'navigation_thread', 'shutdown_thread'):
            thread = getattr(self, name, None)
            if thread is not None and thread.is_alive():
                self.root.after(100, self.finish_quit)
                return
        if getattr(self, 'shutdown_started', False):
            try:
                confirmed = self.shutdown_result.get_nowait() is True
            except queue.Empty:
                confirmed = False
            if not confirmed:
                self.hold_quit('送信状態を確認できないため終了を保留しました。自動再開はOFFです。')
                return
        else:
            try:
                from queue_state import pending
                rows = pending(self.db)
                if rows:
                    home = pathlib.Path(self.db.execute('PRAGMA database_list').fetchone()[2]).resolve().parent
                    self.shutdown_result = queue.Queue(maxsize=1)
                    def worker():
                        try:
                            confirmed = reconcile_pending_shutdown(home)
                        except Exception:
                            confirmed = False
                        self.shutdown_result.put_nowait(confirmed)
                    self.shutdown_thread = threading.Thread(target=worker, daemon=False,
                                                            name='resume-shutdown')
                    self.shutdown_started = True
                    self.shutdown_thread.start()
                    self.root.after(100, self.finish_quit)
                    return
            except Exception:
                self.hold_quit('送信状態を確認できないため終了を保留しました。自動再開はOFFです。')
                return
        if self.tray:
            try: self.tray.stop()
            except Exception:
                self.hold_quit('常駐アイコンを終了できません。アプリは終了していません。')
                return
        self.db.close()
        # Native tray teardown and Tk's own mainloop have separate stop flags.
        self.root.quit()
        self.root.destroy()

    def run(self): self.root.mainloop()


def main():
    p = JapaneseParser(description='Codexの選択したチャットと監視状態を管理します')
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('gui'); a.add_argument('--no-tray', action='store_true', help='常駐アイコンなしでGUIを起動')
    a = sub.add_parser('diagnose', help='画面を開かず起動環境を診断する')
    a.add_argument('--json', action='store_true', help='機械可読の診断結果')
    for command in ('scan', 'dry-run', 'list'): sub.add_parser(command)
    for command in ('enable', 'disable'):
        a = sub.add_parser(command); a.add_argument('id')
    a = sub.add_parser('quota-test'); a.add_argument('id'); a.add_argument('failure_turn'); a.add_argument('reset_at', type=int); a.add_argument('--limit', choices=['5h', 'weekly'], default='5h')
    a = sub.add_parser('mark'); a.add_argument('id'); a.add_argument('state', choices=['completed', 'blocked', 'idle'])
    review = a.add_mutually_exclusive_group()
    review.add_argument('--expected-generation', type=int, metavar='世代',
                        help='idleの選び直しで必須。人間が確認した0以上の世代を指定する')
    review.add_argument('--dry-run', action='store_true',
                        help='idleの選び直し前に、状態・選択・世代・可否だけを変更せず確認する')
    args = p.parse_args(['gui'] if len(sys.argv) == 1 else None)
    if args.cmd == 'diagnose':
        from diagnose import main as diagnose_main
        return diagnose_main(['--json'] if args.json else [])
    if args.cmd == 'gui':
        with gui_lease() as acquired:
            if not acquired:
                if sys.stderr is not None:
                    print('同じ保存先のCodex Safe Resumeは起動済み、または設定削除中です。起動済みの場合は常駐アイコンから表示できます。', file=sys.stderr)
                return 0
            return GUI(enable_tray=not args.no_tray).run()
    if args.cmd == 'mark':
        if args.state == 'idle':
            if not args.dry_run and (args.expected_generation is None or args.expected_generation < 0):
                p.exit(2, 'idleの選び直しには、確認済みの0以上の世代を--expected-generationで指定してください。\n')
        elif args.expected_generation is not None or args.dry_run:
            p.exit(2, '--expected-generationと--dry-runはidleの選び直しだけに指定できます。\n')
        try:
            tid = check_id(args.id)
        except ValueError:
            p.exit(2, '対象のチャットIDを確認してください。\n')
        if args.state == 'idle':
            # 選び直しの拒否前にdiscover/syncで状態を変更しない。
            with connection() as db:
                if args.dry_run:
                    row = db.execute('SELECT state,enabled,generation FROM threads WHERE id=?', (tid,)).fetchone()
                    if row is None:
                        p.exit(2, '指定したチャットは登録されていません。\n')
                    print(json.dumps({'状態': row[0], '選択': bool(row[1]), '世代': row[2],
                                      '選び直し可能': row[0] in ('blocked', 'completed') and not row[1]},
                                     ensure_ascii=False))
                    return
                try:
                    mark(db, tid, args.state, expected_generation=args.expected_generation)
                except ValueError:
                    p.exit(2, '確認した世代または現在の状態が一致しません。対象の状態を確認し直してください。\n')
            return
    with connection() as db:
        scanned, error = discover()
        if scanned: sync(db, scanned)
        if error: print('読み取りエラー:', error, file=sys.stderr)
        if args.cmd == 'scan': print(json.dumps({'検出件数': len(scanned)}, ensure_ascii=False))
        elif args.cmd == 'list': print(json.dumps([{'選択': bool(r['enabled']), '状態': r['state']} for r in list_threads(db)], ensure_ascii=False))
        elif args.cmd == 'dry-run': print(json.dumps({'候補件数': len(dry_run(db, scanned))}, ensure_ascii=False))
        elif args.cmd in ('enable', 'disable'): enable(db, args.id, args.cmd == 'enable')
        elif args.cmd == 'quota-test': quota(db, args.id, args.failure_turn, args.reset_at, args.limit)
        elif args.cmd == 'mark':
            try:
                mark(db, tid, args.state)
            except ValueError:
                p.exit(2, '指定したチャットを記録できません。対象の状態を確認してください。\n')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        if sys.stderr is not None:
            print('処理を完了できません。接続・権限・画面ライブラリを確認してください。', file=sys.stderr)
        raise SystemExit(1)
