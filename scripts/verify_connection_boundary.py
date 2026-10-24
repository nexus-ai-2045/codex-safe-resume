"""空の隔離環境でstdio→共有接続を検査する。Desktop・認証・モデルは使わない。"""
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import websocket
from cli_security import validate_location
from connection_settings import prepare, load, enabled
from core import connection
from native_proxy import NativeProxySocket


def verify(cli, bridge=None, *, features=()):
    features = tuple(features)
    if any(name not in ('code_mode_host', 'windows_sandbox_service') for name in features):
        raise ValueError('未対応の検査用featureです')
    feature_args = [part for name in features for part in ('-c', f'features.{name}=true')]
    cli = validate_location(cli)
    if bridge is not None:
        bridge = validate_location(bridge)
    report = dict(test='stdio_shared_boundary', real_desktop=False, real_user_chats=False,
                  auth_copied=False, turn_start_requests=0, synthetic_thread_in_isolated_state=True,
                  requested_features=list(features), success=False)
    stage = 'isolated_server'
    owned, raw, ws, child = [], None, None, None
    # Short paths keep the native Unix socket below platform address limits.
    location = '/tmp' if os.name == 'posix' else None
    with tempfile.TemporaryDirectory(prefix='csr-', dir=location) as folder:
        home = Path(folder).resolve()
        work = home/'w'; work.mkdir()
        state = home/'resume'
        # No API keys, existing auth, user config, hooks or profile are inherited.
        env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG')
               if key in os.environ}
        env.update(CODEX_HOME=str(home), CODEX_RESUME_HOME=str(state), RUST_LOG='error')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        try:
            server = subprocess.Popen([str(cli), *feature_args, '-c', 'cli_auth_credentials_store="file"',
                                       'app-server', '--listen', 'unix://'], env=env, cwd=work,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, **options)
            owned.append(server)
            for _ in range(20):
                version = subprocess.run([str(cli), 'app-server', 'daemon', 'version'], env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, timeout=4, **options)
                if version.returncode == 0 or server.poll() is not None:
                    break
                time.sleep(.15)
            if version.returncode:
                raise ValueError('isolated server unavailable')
            with closing(connection(state)) as db:
                prepare(db, cli)
                report['preparation_does_not_enable_resume'] = not enabled(load(db))
            command = [str(bridge)] if bridge else [sys.executable, str(ROOT/'desktop_launcher.py')]
            stage = 'stdio_initialize'
            child = subprocess.Popen([*command, *feature_args, 'app-server', '--analytics-default-enabled'], env=env,
                                     cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, **options)
            owned.append(child)
            received = queue.Queue(maxsize=256)
            def collect():
                try:
                    while True:
                        line = child.stdout.readline(4 * 1024 * 1024 + 1)
                        if not line:
                            break
                        if len(line) > 4 * 1024 * 1024:
                            raise ValueError('large reply')
                        received.put_nowait(json.loads(line))
                except Exception:
                    pass
                finally:
                    try: received.put_nowait(None)
                    except queue.Full: pass
            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            def rpc(ident, method, params):
                # These are the only mutations: one synthetic thread in temporary empty state.
                if method not in ('initialize', 'thread/start'):
                    raise ValueError('unexpected test method')
                child.stdin.write((json.dumps(dict(id=ident, method=method, params=params))+'\n').encode())
                child.stdin.flush()
                # onefileの初回展開を含む検査期限。通信側の期限は変更しない。
                deadline = time.monotonic() + 45
                while True:
                    message = received.get(timeout=max(.01, deadline-time.monotonic()))
                    if message is None:
                        raise ValueError('stdio closed')
                    if message.get('id') == ident:
                        if 'error' in message:
                            raise ValueError('request rejected')
                        return message['result']
            result = rpc(101, 'initialize', {'clientInfo': {'name': 'safe_resume_stdio_boundary_probe',
                         'version': '0.5.0'}, 'capabilities': {'experimentalApi': True}})
            report['stdio_initialize'] = isinstance(result.get('userAgent'), str)
            child.stdin.write(b'{"method":"initialized","params":{}}\n'); child.stdin.flush()
            stage = 'isolated_thread'
            result = rpc(102, 'thread/start', {'cwd': str(work), 'sandbox': 'read-only', 'ephemeral': True})
            tid = result['thread']['id']
            stage = 'app_connection'
            raw = NativeProxySocket(cli, timeout=5, codex_home=home)
            ws = websocket.WebSocket(); ws.connect('ws://localhost/rpc', socket=raw, timeout=5)
            ws.send(json.dumps({'id': 201, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'safe_resume_app_boundary_probe', 'version': '0.5.0'},
                'capabilities': {'experimentalApi': True}}}))
            if 'result' not in json.loads(ws.recv()):
                raise ValueError('initialize rejected')
            ws.send(json.dumps({'method': 'initialized', 'params': {}}))
            ws.send(json.dumps({'id': 202, 'method': 'thread/read',
                                'params': {'threadId': tid, 'includeTurns': False}}))
            while True:
                observed = json.loads(ws.recv())
                if observed.get('id') == 202:
                    break
            report['same_isolated_thread_visible'] = ((observed.get('result') or {}).get('thread') or {}).get('id') == tid
            # A thread without user turns cannot exercise history reconciliation.
            # Do not send a prompt merely to turn this transport smoke green.
            report['live_turn_with_actual_turn'] = 'not_tested'
            report['success'] = (report['stdio_initialize'] and report['same_isolated_thread_visible']
                                 and report['preparation_does_not_enable_resume'])
        except Exception as error:
            report.update(failed_stage=stage, error_type=type(error).__name__)
        finally:
            if ws:
                try: ws.close(timeout=.2)
                except Exception: pass
            if raw:
                try: raw.close()
                except Exception: report['success'] = False
            if child and child.stdin:
                try: child.stdin.close()
                except Exception: pass
            for process in reversed(owned):
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
                if process.stdout: process.stdout.close()
            report['owned_processes_reaped'] = all(process.poll() is not None for process in owned)
            report['success'] = report['success'] and report['owned_processes_reaped']
    return report


def main():
    from diagnostics import JapaneseParser
    parser = JapaneseParser(description='隔離したCLIのstdioと共有接続を検査します。実チャットへは送信しません。')
    parser.add_argument('--codex-cli', required=True, help='公式CLIの実行ファイル')
    parser.add_argument('--desktop-bridge', help='配布物の接続用プログラム。省略時はsource版')
    parser.add_argument('--dry-run', action='store_true', help='配置を検査し、プロセスを起動せず予定だけを表示')
    parser.add_argument('--feature', action='append', default=[],
                        choices=('code_mode_host', 'windows_sandbox_service'),
                        help='隔離サーバーとDesktop起動形の双方で検査する既知feature。繰返し指定可')
    args = parser.parse_args()
    if args.dry_run:
        try:
            validate_location(args.codex_cli)
            if args.desktop_bridge:
                validate_location(args.desktop_bridge)
        except Exception as error:
            print(json.dumps(dict(status='blocked', failed_stage='executable_validation',
                                  error_type=type(error).__name__), sort_keys=True))
            return 1
        print(json.dumps(dict(status='dry_run', processes_started=0, real_desktop=False,
                              turn_start_requests=0, requested_features=args.feature, steps=['isolated_server', 'stdio_initialize',
                              'isolated_thread', 'shared_readback', 'cleanup']), sort_keys=True))
        return 0
    try:
        result = verify(args.codex_cli, args.desktop_bridge, features=args.feature)
    except Exception as error:
        result = dict(success=False, failed_stage='executable_validation', error_type=type(error).__name__)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
