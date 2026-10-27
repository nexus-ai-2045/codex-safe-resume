"""Desktopが使う接続用CLI。既存共有サーバーへ中継し、自動起動しない。"""
from contextlib import closing
from pathlib import Path
import os
import re
import subprocess
import sys

from core import connection, control_socket
from connection_settings import load, validate_executable
from desktop_bridge import DesktopBridge
from desktop_stdio import relay


def _options(argv):
    features = set()
    command_seen = False
    remaining = iter(argv)
    supported = {
        'features.windows_sandbox_service=true': 'windows_sandbox_service',
        'features.code_mode_host=true': 'code_mode_host',
        'features.prefer_mxc=true': 'prefer_mxc',
    }
    for argument in remaining:
        if argument == 'app-server' and not command_seen:
            command_seen = True
            continue
        if argument in ('-c', '--config'):
            value = next(remaining, None)
            if value in supported:
                features.add(supported[value])
                continue
            if value in {name + '=true' for name in _PLUGIN_SETTINGS}:
                features.add(value.removesuffix('=true'))
                continue
        if command_seen:
            if argument in ('--analytics-default-enabled', '--stdio'):
                continue
            if argument == '--listen' and next(remaining, None) == 'stdio://':
                continue
        raise ValueError('共有サーバーへ適用できない起動設定があります')
    if not command_seen:
        raise ValueError('接続用の操作を確認してください')
    return frozenset(features)


_PLUGIN_SETTINGS = frozenset({
    'plugins.codex-app-tools' '@openai-bundled.mcp_servers.codex_app.enabled',
    'plugins.code-review' '@openai-bundled.mcp_servers.code-review.enabled',
})


def check_server_settings(probe, requested):
    if not requested:
        return
    reply = probe.request('config/read', {'includeLayers': False})
    server_config = reply.get('config') if isinstance(reply, dict) else None
    for name in requested:
        keys = name.split('.') if name in _PLUGIN_SETTINGS else ['features', name]
        value = server_config
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not True:
            raise ValueError('共有サーバーの要求された機能設定を確認してください')


def _require_stdio_command(argv):
    remaining = iter(argv)
    for argument in remaining:
        if argument in ('-c', '--config'):
            if next(remaining, None) is None:
                break
            continue
        if argument != 'app-server':
            break
        for option in remaining:
            if option in ('-c', '--config'):
                if next(remaining, None) is None:
                    raise ValueError('接続用の操作を確認してください')
            elif option == 'app-server':
                raise ValueError('接続用の操作を確認してください')
            elif option == '--listen':
                if next(remaining, None) != 'stdio://':
                    raise ValueError('接続用の操作を確認してください')
            elif option.startswith('--listen=') and option != '--listen=stdio://':
                raise ValueError('接続用の操作を確認してください')
        return
    raise ValueError('接続用の操作を確認してください')


def _fallback_stdio(config, argv, reason):
    from desktop_binding import record_startup_fallback, validate_startup_caller
    validate_startup_caller(config)
    try:
        record_startup_fallback(config, reason)
    except Exception:
        pass
    executable = validate_executable(config, require_confirmation=False)
    if getattr(sys, 'frozen', False) and Path(executable).samefile(sys.executable):
        raise ValueError('接続用プログラム自身を公式CLIには指定できません')
    return subprocess.run(
        [str(executable), *argv], check=False,
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}),
    ).returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '--open-chat':
        from thread_navigation import mac_open_chat
        status = mac_open_chat(argv[1], argv[2]) if len(argv) == 3 else 'failed'
        print(status, flush=True)
        return 0
    if (argv and argv[0] == '--manage-desktop') or os.environ.get('CSR_WINDOWS_ORDER_FREE') == '1':
        raise ValueError('Codexを起動し直す接続方式は利用できません')
    with closing(connection()) as db:
        config = load(db)
    executable = validate_executable(config, require_confirmation=False)
    if getattr(sys, 'frozen', False) and Path(executable).samefile(sys.executable):
        raise ValueError('接続用プログラム自身を公式CLIには指定できません')
    if argv in (['--version'], ['-V']):
        result = subprocess.run([str(executable), '--version'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=8, check=True,
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        text = result.stdout.decode('utf-8').strip()
        if not re.fullmatch(r'codex-cli [0-9]+\.[0-9]+\.[0-9]+[-+.0-9A-Za-z]*', text):
            raise ValueError('公式CLIの版を確認できません')
        print(text)
        return 0
    managed = config.get('mode') == 'managed'
    if managed:
        if not config.get('launch_nonce') or os.environ.get('CSR_DESKTOP_NONCE') != config['launch_nonce']:
            raise ValueError('Desktopの起動要求が一致しません')
        from desktop_binding import validate_startup_caller
        validate_startup_caller(config)
        _require_stdio_command(argv)
    try:
        requested_features = _options(argv)
    except ValueError:
        if managed:
            return _fallback_stdio(config, argv, 'unsupported_launch_options')
        raise
    failure_reason = 'shared_transport_unavailable'
    try:
        with DesktopBridge(config=config, transport_only=True) as probe:
            failure_reason = 'shared_settings_mismatch'
            check_server_settings(probe, requested_features)
            failure_reason = 'shared_transport_unavailable'
    except Exception:
        if managed:
            return _fallback_stdio(config, argv, failure_reason)
        raise
    executable = validate_executable(config, require_confirmation=False)
    if managed:
        from desktop_binding import RelayReceipt
        receipt = None
        try:
            receipt = RelayReceipt(config)
            receipt = receipt.start()
        except Exception:
            if receipt is not None:
                try:
                    receipt.close()
                except Exception:
                    pass
            return _fallback_stdio(config, argv, 'receipt_unavailable')
        try:
            relay(executable, codex_home=control_socket().parent.parent, receipt=receipt)
        finally:
            receipt.close()
    else:
        relay(executable, codex_home=control_socket().parent.parent)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print('Desktopの共有接続を確認できません。接続の準備と共有サーバーを確認してください。', file=sys.stderr)
        raise SystemExit(1)
