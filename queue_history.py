from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import sqlite3
import stat

from core import check_id, is_quota_error, reject_links


MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_ROWS = 256
SCHEMAS = {
    'state': {'threads': {'id', 'rollout_path', 'archived'}},
    'thread_history': {
        'thread_turns': {'thread_id', 'turn_id', 'rollout_ordinal', 'status', 'error_json', 'first_user_item_id'},
        'thread_items': {'thread_id', 'turn_id', 'item_id', 'item_type', 'item_json'},
        'thread_history_projection_state': {'thread_id', 'next_rollout_byte_offset'},
    },
    'queue': {'queued_items': {'id', 'thread_id', 'payload_json'}},
}
UNKNOWN = {'delivery': 'unknown'}


def continuation_text(attempt_id):
    return '続けて\n[Safe Resume: ' + check_id(attempt_id) + ']'


def _json(value):
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('履歴の形式を確認できません')
    def distinct(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('履歴の形式を確認できません')
            result[key] = item
        return result

    def invalid_constant(value):
        raise ValueError('履歴の形式を確認できません')

    result = json.loads(value, object_pairs_hook=distinct, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError('履歴の形式を確認できません')
    return result


def _identity(path, *, database=False):
    reject_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('履歴ファイルを確認できません')
    if os.name == 'posix' and info.st_uid != os.getuid():
        raise ValueError('履歴ファイルを確認できません')
    if database and info.st_size > MAX_DATABASE_BYTES:
        raise ValueError('履歴ファイルの上限を超えています')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class QueueHistory:
    def __init__(self, codex_home):
        self.home = pathlib.Path(codex_home).expanduser().absolute()

    def _database(self, kind):
        reject_links(self.home)
        pattern = re.compile(re.escape(kind) + r'_(\d+)\.sqlite\Z')
        candidates = [(int(match.group(1)), path) for path in self.home.iterdir()
                      if (match := pattern.fullmatch(path.name))]
        if not candidates:
            raise ValueError('履歴データベースを確認できません')
        return max(candidates, key=lambda pair: pair[0])[1]

    @contextlib.contextmanager
    def _db(self, kind):
        path = self._database(kind)
        before = _identity(path, database=True)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = pathlib.Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _identity(sidecar, database=True)
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA busy_timeout=100')
            db.execute('PRAGMA trusted_schema=OFF')
            remaining = [10000]

            def bounded():
                remaining[0] -= 1
                return int(remaining[0] <= 0)

            db.set_progress_handler(bounded, 1000)
            db.execute('BEGIN')
            for table, required in SCHEMAS[kind].items():
                row = db.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
                if row is None or row[0] != 'table':
                    raise ValueError('履歴の対応形式を確認できません')
                columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
                if not required <= columns:
                    raise ValueError('履歴の対応形式を確認できません')
            yield db
            if _identity(path, database=True) != before:
                raise ValueError('履歴の更新を確認してください')

    def _rollout(self, thread_id):
        with self._db('state') as db:
            rows = db.execute('SELECT rollout_path,archived FROM threads WHERE id=? LIMIT 2', (thread_id,)).fetchall()
        if len(rows) != 1 or rows[0]['archived'] != 0:
            raise ValueError('対象チャットを確認できません')
        raw = rows[0]['rollout_path']
        if not isinstance(raw, str) or not raw or '\x00' in raw:
            raise ValueError('履歴ファイルを確認できません')
        if raw.startswith('\\\\?\\'):
            raw = raw[4:]
        path = pathlib.Path(raw)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('履歴ファイルを確認できません')
        relative = path.relative_to(self.home / 'sessions')
        if not relative.parts or not path.name.endswith('-' + thread_id + '.jsonl'):
            raise ValueError('履歴ファイルを確認できません')
        return path, _identity(path)

    def _current(self, db, thread_id, identity):
        rows = db.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=? LIMIT 2', (thread_id,)).fetchall()
        return len(rows) == 1 and type(rows[0][0]) is int and rows[0][0] == identity[2]

    def candidate(self, thread_id):
        unknown = {'latest_turn': None, 'status': 'unknown', 'error': None,
                   'failure': False, 'projection_current': False, 'observation_verified': False}
        try:
            thread_id = check_id(thread_id)
            path, identity = self._rollout(thread_id)
            with self._db('thread_history') as db:
                if not self._current(db, thread_id, identity):
                    return unknown
                rows = db.execute('SELECT turn_id,status,CASE WHEN error_json IS NULL OR length(error_json)<=? THEN error_json ELSE ? END AS error_json,rollout_ordinal FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 2', (MAX_JSON_BYTES, '', thread_id)).fetchall()
                if not rows or (len(rows) == 2 and rows[0]['rollout_ordinal'] == rows[1]['rollout_ordinal']):
                    return unknown
                row = rows[0]
                turn = check_id(row['turn_id'])
                if type(row['rollout_ordinal']) is not int or row['rollout_ordinal'] < 0:
                    return unknown
                if row['status'] not in {'inProgress', 'completed', 'interrupted', 'failed'}:
                    return unknown
                error = _json(row['error_json']) if row['error_json'] is not None else None
                result = {'latest_turn': turn, 'status': row['status'], 'error': error,
                          'failure': row['status'] == 'failed' and is_quota_error(error),
                          'projection_current': True, 'observation_verified': False}
            if _identity(path) != identity:
                return unknown
            return result
        except (OSError, ValueError, RuntimeError, sqlite3.Error, UnicodeError, RecursionError):
            return unknown

    def pending(self, thread_id):
        thread_id = check_id(thread_id)
        with self._db('queue') as db:
            rows = db.execute('SELECT id,CASE WHEN length(payload_json)<=? THEN payload_json ELSE NULL END AS payload_json FROM queued_items WHERE thread_id=? LIMIT ?', (MAX_JSON_BYTES, thread_id, MAX_ROWS + 1)).fetchall()
            if len(rows) > MAX_ROWS:
                raise ValueError('待機項目を確認できません')
            result = []
            for row in rows:
                payload = _json(row['payload_json'])
                if set(payload) != {'UserInput'} or not isinstance(payload['UserInput'], dict):
                    raise ValueError('待機項目の形式を確認できません')
                value = payload['UserInput']
                content = value.get('content')
                client = value.get('client_id')
                if not isinstance(content, list) or not content or len(content) > MAX_ROWS:
                    raise ValueError('待機項目の形式を確認できません')
                if client is not None and (not isinstance(client, str) or not client or len(client) > 128):
                    raise ValueError('待機項目の対応を確認できません')
                result.append({'queue_id': check_id(row['id']), 'client_id': client, 'input': content})
        return result

    def receipt(self, thread_id, attempt_id, queue_id=None, client_id=None):
        try:
            thread_id = check_id(thread_id)
            text = continuation_text(attempt_id)
            client_id = check_id(client_id if client_id is not None else attempt_id)
            if queue_id is not None:
                queue_id = check_id(queue_id)
            path, identity = self._rollout(thread_id)
            with self._db('thread_history') as db:
                if not self._current(db, thread_id, identity):
                    return dict(UNKNOWN)
                rows = db.execute("SELECT CASE WHEN length(i.item_json)<=? THEN i.item_json ELSE NULL END AS item_json,i.item_id,i.turn_id,t.first_user_item_id FROM thread_items i JOIN thread_turns t ON t.thread_id=i.thread_id AND t.turn_id=i.turn_id WHERE i.thread_id=? AND i.item_type='userMessage' AND (instr(i.item_json,?)>0 OR instr(i.item_json,?)>0) LIMIT ?", (MAX_JSON_BYTES, thread_id, check_id(attempt_id), client_id, MAX_ROWS + 1)).fetchall()
                if len(rows) > MAX_ROWS:
                    return dict(UNKNOWN)
                matches = []
                expected = [{'type': 'text', 'text': text}]
                for row in rows:
                    payload = _json(row['item_json'])
                    if not isinstance(row['item_id'], str) or not row['item_id'] or len(row['item_id']) > 128:
                        return dict(UNKNOWN)
                    if payload.get('type') != 'userMessage' or payload.get('id') != row['item_id']:
                        return dict(UNKNOWN)
                    if payload.get('content') == expected or payload.get('clientId') == client_id:
                        if row['first_user_item_id'] != row['item_id']:
                            return dict(UNKNOWN)
                        matches.append(check_id(row['turn_id']))
            if _identity(path) != identity:
                return dict(UNKNOWN)
            if len(matches) == 1:
                return {'delivery': 'reached', 'turn': {'id': matches[0]}}
            if matches:
                return dict(UNKNOWN)
            pending = [row for row in self.pending(thread_id) if (row['input'] == [{'type': 'text', 'text': text}] or row['client_id'] == client_id)
                       and (queue_id is None or row['queue_id'] == queue_id)]
            return {'delivery': 'pending'} if len(pending) == 1 else dict(UNKNOWN)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, UnicodeError, RecursionError):
            return dict(UNKNOWN)

