"""Codex Desktop 接続前診断。標準出力に個人情報・ローカルパスを出さない。"""
import argparse
from contextlib import closing
import importlib.util
import json
import os
import pathlib
import platform
import sqlite3
import stat
import sys


def inspection(codex_home=None, state_home=None, os_name=None):
    """読み取り専用。どのファイルも作成せず、ユーザー固有値を返さない。"""
    os_name = os_name or ('Windows' if os.name == 'nt' else 'macOS' if sys.platform == 'darwin' else 'Other')
    home = pathlib.Path(codex_home) if codex_home is not None else pathlib.Path(os.environ.get('CODEX_HOME', pathlib.Path.home() / '.codex'))
    state_home = pathlib.Path(state_home) if state_home is not None else pathlib.Path(os.environ.get('CODEX_RESUME_HOME', pathlib.Path.home() / '.codex-safe-resume'))
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
        checks['control_socket'] = 'unsafe_symlink'
    elif socket_path.exists():
        checks['control_socket'] = 'present'
    else:
        checks['control_socket'] = 'unavailable'
    checks['websocket_client'] = 'available' if importlib.util.find_spec('websocket') else 'missing'
    checks['tray_library'] = 'available' if importlib.util.find_spec('pystray') else 'missing'
    checks['pillow'] = 'available' if importlib.util.find_spec('PIL') else 'missing'
    try:
        import tkinter  # noqa: F401
        checks['tkinter'] = 'available'
    except (ImportError, RuntimeError):
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


def main():
    parser = argparse.ArgumentParser(description='Codex 自動再開の読み取り専用診断。個人情報は出力しません。')
    parser.add_argument('--json', action='store_true', help='機械可読の診断結果')
    args = parser.parse_args()
    result = inspection()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print('Codex Safe Resume — 実機接続前の診断（読み取り専用）')
        for k, v in result['checks'].items():
            print(f'{k}: {v}')
        print('実際の再開送信と5時間枠の解除検証は、未実施です。')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())