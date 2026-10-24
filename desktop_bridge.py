"""既存Desktopへの接続と、再照合を伴う一度だけの再開要求。"""
from collections import deque
import json
import os
import subprocess
import time
from core import control_socket, check_id, observe_resume_quota, is_quota_error
from connection_settings import validate_executable, enabled as configuration_enabled
from native_proxy import NativeProxySocket


class BridgeError(RuntimeError):
    pass


class DesktopBridge:
    def __init__(self, home=None, timeout=8, config=None, *, transport_only=False):
        self.path = control_socket(home)
        self.timeout = timeout
        self.ws = None
        self.sequence = 0
        self.events = deque(maxlen=256)
        self.attempted_failures = set()
        self.config = config or {}
        self.connected = False
        self.raw = None
        self.observation_marks = {}
        self.transport_only = transport_only

    def capabilities(self):
        # Guarded mode rechecks state but does not lock other Desktop writers.
        active = self.connected and not self.transport_only and configuration_enabled(self.config)
        if self.config.get('mode') == 'managed':
            from desktop_binding import live_binding
            active = active and live_binding(self.config, self.path)
        return {'safe_admission': False, 'guarded_admission': active,
                'desktop_binding': active,
                'binding_source': ('live_desktop_stdio_receipt' if self.config.get('mode') == 'managed'
                                   else 'operator_confirmed_shared_daemon'),
                'reason': 'guarded' if active else 'connection_setup_required'}

    def __enter__(self):
        try:
            executable = validate_executable(self.config, require_confirmation=not self.transport_only)
            env = dict(os.environ, CODEX_HOME=str(self.path.parent.parent))
            result = subprocess.run([str(executable), 'app-server', 'daemon', 'version'],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=self.timeout,
                                    env=env, check=True,
                                    **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            if len(result.stdout) > 65536:
                raise BridgeError('接続先の版情報を確認できません')
            version = json.loads(result.stdout)
            cli_version, server_version = version.get('cliVersion'), version.get('appServerVersion')
            if (not isinstance(cli_version, str) or not cli_version or
                    not isinstance(server_version, str) or cli_version != server_version):
                raise BridgeError('CLIと共有サーバーの版を確認してください')
            # Version probing must not silently adopt a CLI update.
            validate_executable(self.config, require_confirmation=not self.transport_only)
            import websocket
            self.raw = NativeProxySocket(executable, timeout=self.timeout,
                                         codex_home=self.path.parent.parent)
            self.ws = websocket.WebSocket(skip_utf8_validation=False)
            self.ws.connect('ws://localhost/rpc', socket=self.raw, timeout=self.timeout)
            initialized = self.request('initialize', {
                'clientInfo': {'name': 'codex_safe_resume', 'title': 'Codex Safe Resume', 'version': '0.5.0'},
                'capabilities': {'experimentalApi': True}})
            if not isinstance(initialized.get('userAgent'), str) or not initialized['userAgent']:
                raise BridgeError('共有サーバーの初期応答を確認できません')
            self.ws.send(json.dumps({'method': 'initialized', 'params': {}}))
            self.connected = True
        except Exception:
            self.__exit__()
            raise BridgeError('共有サーバーへの接続を確認できません。接続設定とDesktop側を確認してください') from None
        return self

    def __exit__(self, *_):
        self.connected = False
        try:
            if self.ws:
                self.ws.close(timeout=0.2)
        finally:
            if self.raw:
                self.raw.close()

    def prepare_thread(self, thread_id):
        """選択済みの既存チャットだけをロードする。入力は送らない。"""
        thread_id = check_id(thread_id)
        result = self.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
        thread = result.get('thread') or {}
        if thread.get('id') != thread_id:
            raise BridgeError('対象チャットを確認できません')
        state = thread.get('status') or {}
        if state.get('type') == 'notLoaded':
            resumed = self.request('thread/resume', {'threadId': thread_id})
            if (resumed.get('thread') or {}).get('id') != thread_id:
                raise BridgeError('既存チャットの読込みを確認できません')

    def request(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError('Desktopの応答を確認できません')
            self.ws.settimeout(remaining)
            raw = self.ws.recv()
            if not isinstance(raw, str) or len(raw) > 4 * 1024 * 1024:
                raise BridgeError('Desktopの応答形式を確認できません')
            try:
                reply = json.loads(raw)
            except (ValueError, TypeError):
                raise BridgeError('Desktopの応答形式を確認できません') from None
            if not isinstance(reply, dict):
                raise BridgeError('Desktopの応答形式を確認できません')
            if reply.get('id') != ident or 'method' in reply:
                if 'method' in reply:
                    if len(self.events) == self.events.maxlen:
                        raise BridgeError('Desktopのイベント回収が追いつきません')
                    self.events.append(reply)
                    if 'id' in reply:
                        # Approval and user-input requests are never auto-approved.
                        raise BridgeError('承認または入力を待っています')
                continue
            if 'error' in reply or not isinstance(reply.get('result'), dict):
                raise BridgeError('Desktopから確認可能な応答を得られません')
            return reply['result']

    def quota(self):
        return self.request('account/rateLimits/read')

    def snapshot(self, thread_id):
        thread_id = check_id(thread_id)
        result = self.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
        thread = result.get('thread')
        if not isinstance(thread, dict) or thread.get('id') != thread_id:
            raise BridgeError('対象チャットの一致を確認できません')
        # Persisted thread history may omit an active turn. This endpoint merges
        # the live snapshot and orders by chronology, never by turn-ID text.
        live = self.request('thread/turns/list', {'threadId': thread_id,
                                                'sortDirection': 'desc', 'limit': 1})
        turns = live.get('data')
        state = thread.get('status')
        if not isinstance(state, dict) or state.get('type') not in ('idle', 'active'):
            raise BridgeError('チャットの実行状態を確認できません')
        if not isinstance(turns, list) or len(turns) != 1 or not isinstance(turns[0], dict):
            raise BridgeError('最新ターンを確認できません')
        latest = turns[0]
        tid = latest.get('id')
        status = latest.get('status')
        if not isinstance(tid, str) or not tid or status not in ('failed', 'completed', 'interrupted', 'inProgress'):
            raise BridgeError('最新ターンを確認できません')
        flags = state.get('activeFlags', [])
        if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
            raise BridgeError('承認待ちの状態を確認できません')
        if any(flag not in ('waitingOnApproval', 'waitingOnUserInput') for flag in flags):
            raise BridgeError('実行状態に未対応の情報があります')
        limited = status == 'failed' and is_quota_error(latest.get('error'))
        self.observation_marks.setdefault((thread_id, tid), len(self.events))
        return dict(id=thread_id, title='', cwd=thread.get('cwd', ''), latest_turn=tid,
                    turn_status=status, limited=limited, failure_turn=tid if limited else None,
                    awaiting=bool(flags), idle=state['type'] == 'idle', observation_verified=True)

    def send_once(self, thread_id, expected_turn, command='続けて', *, attempt_id=None):
        thread_id = check_id(thread_id)
        capabilities = self.capabilities()
        if (capabilities.get('desktop_binding') is not True or
                not (capabilities.get('safe_admission') is True or
                     capabilities.get('guarded_admission') is True)):
            raise BridgeError('安全な送信能力とDesktopの対応を確認できません')
        if command != '続けて' or (thread_id, expected_turn) in self.attempted_failures:
            raise BridgeError('同じ停止への再送または未対応の指示を拒否しました')
        try:
            allowed, _ = observe_resume_quota(self.quota())
        except (ValueError, TypeError):
            raise BridgeError('利用枠と利用許可を確認できません') from None
        if not allowed:
            raise BridgeError('利用枠の解除と利用許可を確認できません')
        current = self.snapshot(thread_id)
        if (current['latest_turn'] != expected_turn or not current['limited'] or
                current['awaiting'] or current.get('idle') is not True):
            raise BridgeError('対象の制限停止ターンを確認できません')
        mark = self.observation_marks.get((thread_id, expected_turn), 0)
        for event in list(self.events)[mark:]:
            params = event.get('params')
            if (isinstance(params, dict) and params.get('threadId') == thread_id and
                    str(event.get('method', '')).startswith(('turn/', 'thread/', 'item/'))):
                raise BridgeError('観測後にチャットの変更を検出しました')
        try:
            attempt_id = check_id(attempt_id)
        except ValueError:
            raise BridgeError('送信前に保存した試行IDが必要です') from None
        # This is an application-side guard, not an atomic server-side CAS.
        # Record before writing; a lost reply must never cause another write.
        if self.config.get('mode') == 'managed' and not self.capabilities()['desktop_binding']:
            raise BridgeError('Desktop接続の生存証明が失効しました')
        self.attempted_failures.add((thread_id, expected_turn))
        try:
            result = self.request('turn/start', {'threadId': thread_id,
                                  'clientUserMessageId': attempt_id,
                                  'input': [{'type': 'text', 'text': command}]})
            turn = (result.get('turn') or {}).get('id')
            if not isinstance(turn, str) or not turn or turn == expected_turn:
                raise BridgeError('再開ターンの応答を確認できません')
            actual = self.snapshot(thread_id)
            if actual['latest_turn'] != turn:
                raise BridgeError('実際の再開ターンを照合できません')
            return result
        except Exception:
            raise BridgeError('送信結果を照合できません。自動で再送しません') from None
