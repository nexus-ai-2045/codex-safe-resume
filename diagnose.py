"""Codex Desktop 接続前診断。標準出力に個人情報・ローカルパスを出さない。"""
from diagnostics import JapaneseParser
from contextlib import closing
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys


def inspection(codex_home=None, state_home=None, os_name=None):
    """読み取り専用。どのファイルも作成せず、ユーザー固有値を返さない。"""
    os_name = os_name or ('Windows' if os.name == 'nt' else 'macOS' if sys.platform == 'darwin' else 'Other')
    # 明示指定があるときは、利用できない可能性のあるユーザーホームを評価しない。
    if codex_home is None:
        codex_home = os.environ.get('CODEX_HOME')
    if state_home is None:
        state_home = os.environ.get('CODEX_RESUME_HOME')
    home = pathlib.Path(codex_home) if codex_home is not None else pathlib.Path.home() / '.codex'
    state_home = pathlib.Path(state_home) if state_home is not None else pathlib.Path.home() / '.codex-safe-resume'
    checks = {}
    db_path = home / 'state_5.sqlite'
    checks['codex_state_db'] = 'present' if db_path.is_file() and not db_path.is_symlink() else 'unavailable'
    if checks['codex_state_db'] == 'present':
        try:
            # URI mode=ro is essential: no creation or mutation of the Codex DB.
            with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                names = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
            checks['thread_schema'] = 'supported' if {'id', 'title', 'cwd'} <= names else 'unsupported'
        except (sqlite3.Error, OSError, ValueError):
            checks['thread_schema'] = 'unavailable'
    else:
        checks['thread_schema'] = 'unavailable'
    socket_path = home / 'app-server-control' / 'app-server-control.sock'
    if socket_path.is_symlink():
        checks['control_socket'] = 'native_proxy_required'
    elif socket_path.exists():
        checks['control_socket'] = 'present'
    else:
        checks['control_socket'] = 'unavailable'
    checks['websocket_client'] = 'available' if importlib.util.find_spec('websocket') else 'missing'
    checks['tray_library'] = 'available' if importlib.util.find_spec('pystray') else 'missing'
    checks['pillow'] = 'available' if importlib.util.find_spec('PIL') else 'missing'
    try:
        import tkinter
        tkinter.Tcl()  # Tcl初期化データも検査する。Tkウィンドウは作らない。
        checks['tkinter'] = 'available'
    except Exception:
        checks['tkinter'] = 'missing'
    if state_home.is_symlink():
        checks['state_directory'] = 'unsafe_symlink'
    elif not state_home.exists():
        checks['state_directory'] = 'not_created'
    elif os.name == 'posix' and (state_home.stat().st_mode & 0o077):
        checks['state_directory'] = 'permissions_review_required'
    else:
        checks['state_directory'] = 'present'
    # No thread IDs, usernames, network interfaces, usernames, files or plaintext paths.
    return {'schema': 'codex-safe-resume.diagnostics/v1', 'system': os_name, 'checks': checks,
            'desktop_write_validated': False, 'real_quota_e2e_validated': False}


def render_report(result):
    labels = {'codex_state_db': '会話データ', 'thread_schema': '会話データ形式',
              'control_socket': '既存Desktopの接続先', 'websocket_client': '接続ライブラリ',
              'tray_library': '常駐アイコン', 'pillow': '画像ライブラリ',
              'tkinter': '画面ライブラリ', 'state_directory': '状態の保存先'}
    states = {'present': '存在を確認（接続・送信は未検証）', 'unavailable': '確認できません',
              'supported': '形式を確認', 'unsupported': '未対応の形式',
              'unsafe_symlink': 'リンクのため使用できません', 'available': '利用可能',
              'missing': '未導入', 'not_created': '未作成',
              'native_proxy_required': '公式CLIによる接続先の確認が必要です（未検証）',
              'permissions_review_required': '権限の確認が必要です'}
    lines = ['Codex Safe Resume - 実機接続前の診断（読み取り専用）']
    for key, value in result['checks'].items():
        lines.append(f"{labels.get(key, '追加の検査')}: {states.get(value, '確認が必要です')}")
    lines.append('実際の再開送信と5時間・週次枠の解除検証は、未実施です。')
    return '\n'.join(lines)


def main(argv=None):
    parser = JapaneseParser(description='Codex 自動再開の読み取り専用診断。個人情報は出力しません。')
    parser.add_argument('--json', action='store_true', help='機械可読の診断結果')
    args = parser.parse_args(argv)
    result = inspection()
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) if args.json else render_report(result))
    return 0 if result['checks'].get('tkinter') == 'available' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        if sys.stderr is not None:
            print('診断を完了できません。会話データと権限を確認してください。', file=sys.stderr)
        raise SystemExit(1)
