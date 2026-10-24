from __future__ import annotations

from contextlib import contextmanager
import math
import sqlite3
import time

import auto_watch
import core


_STATES = {'sending', 'queued', 'reached', 'cancelled', 'unknown'}
_FIELDS = ('attempt_id', 'thread_id', 'failure_turn', 'generation', 'policy_generation', 'queue_id', 'state', 'created_at')
_SELECT = '''SELECT q.attempt_id,a.thread_id,a.failure_turn,a.generation,q.policy_generation,
q.queue_id,q.state,q.created_at,a.state AS attempt_state,a.resume_turn
FROM queue_dispatches q JOIN attempts a ON a.attempt_id=q.attempt_id'''
_ERROR = '再開試行の対応を確認できません'


@contextmanager
def _write(db):
    owns = not db.in_transaction
    db.execute('BEGIN IMMEDIATE' if owns else 'SAVEPOINT queue_state_write')
    try:
        yield
        db.execute('COMMIT' if owns else 'RELEASE SAVEPOINT queue_state_write')
    except BaseException:
        if owns:
            db.execute('ROLLBACK')
        else:
            db.execute('ROLLBACK TO SAVEPOINT queue_state_write')
            db.execute('RELEASE SAVEPOINT queue_state_write')
        raise


def _schema(db):
    row = db.execute("SELECT type FROM sqlite_master WHERE name='queue_dispatches'").fetchone()
    if row is None:
        return False
    if row[0] != 'table':
        raise ValueError(_ERROR)
    columns = db.execute('PRAGMA table_info(queue_dispatches)').fetchall()
    if {row[1] for row in columns} != {'attempt_id', 'queue_id', 'policy_generation', 'state', 'created_at'}:
        raise ValueError(_ERROR)
    if [row[1] for row in columns if row[5]] != ['attempt_id']:
        raise ValueError(_ERROR)
    return True


def initialise(db):
    with _write(db):
        if not _schema(db):
            db.execute('''CREATE TABLE queue_dispatches (
                attempt_id TEXT PRIMARY KEY NOT NULL,
                queue_id TEXT,
                policy_generation INTEGER,
                state TEXT NOT NULL CHECK(state IN ('sending','queued','reached','cancelled','unknown')),
                created_at REAL NOT NULL)''')
        _schema(db)


def _dicts(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _valid(row):
    core.check_id(row['attempt_id'])
    core.check_id(row['thread_id'])
    if row['queue_id'] is not None:
        core.check_id(row['queue_id'])
    if (not isinstance(row['failure_turn'], str) or not row['failure_turn'] or len(row['failure_turn']) > 128
            or type(row['generation']) is not int or row['generation'] < 0
            or (row['policy_generation'] is not None and (type(row['policy_generation']) is not int or row['policy_generation'] < 0))
            or row['state'] not in _STATES or type(row['created_at']) not in (int, float)
            or not math.isfinite(row['created_at']) or not 0 <= row['created_at'] <= 2 ** 53):
        raise ValueError(_ERROR)
    return row


def _lookup(db, row):
    if not isinstance(row, dict) or not set(_FIELDS) <= row.keys() or not _schema(db):
        raise ValueError(_ERROR)
    _valid(row)
    attempt = core.check_id(row.get('attempt_id'))
    rows = _dicts(db.execute(_SELECT + ' WHERE q.attempt_id=?', (attempt,)))
    if len(rows) != 1:
        raise ValueError(_ERROR)
    current = _valid(rows[0])
    for key in ('attempt_id', 'thread_id', 'failure_turn', 'generation', 'policy_generation', 'created_at'):
        if key not in row or row[key] != current[key]:
            raise ValueError(_ERROR)
    if row.get('queue_id') is not None and row['queue_id'] != current['queue_id']:
        raise ValueError(_ERROR)
    return current


def _authorised(db, row):
    policy = auto_watch.status(db)
    if row['policy_generation'] is None:
        if policy is not None:
            return False
    elif (policy is None or not policy['enabled'] or policy['generation'] != row['policy_generation']):
        return False
    thread = db.execute('SELECT enabled,state,generation,failure_turn FROM threads WHERE id=?', (row['thread_id'],)).fetchone()
    return (thread is not None and thread[0] == 1 and thread[1] == 'claimed'
            and thread[2] == row['generation'] and thread[3] == row['failure_turn']
            and row['attempt_state'] == 'sending' and row['resume_turn'] is None)


def _public(row):
    return {key: row[key] for key in _FIELDS}


def start(db, tid, intent, now=None):
    tid = core.check_id(tid)
    if not isinstance(intent, (tuple, list)) or len(intent) != 4:
        raise ValueError(_ERROR)
    if (not isinstance(intent[0], str) or not intent[0] or len(intent[0]) > 128
            or type(intent[1]) is not int or intent[1] < 0
            or (intent[3] is not None and (type(intent[3]) is not int or intent[3] < 0))):
        raise ValueError(_ERROR)
    attempt = core.check_id(intent[2])
    timestamp = time.time() if now is None else now
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or not 0 <= timestamp <= 2 ** 53:
        raise ValueError(_ERROR)
    with _write(db):
        if not _schema(db):
            raise ValueError(_ERROR)
        original = db.execute('SELECT thread_id,failure_turn,generation,state,resume_turn FROM attempts WHERE attempt_id=?', (attempt,)).fetchone()
        if original is None or (original[0], original[1], original[2]) != (tid, intent[0], intent[1]):
            raise ValueError(_ERROR)
        existing = db.execute('SELECT policy_generation FROM queue_dispatches WHERE attempt_id=?', (attempt,)).fetchone()
        if existing is not None:
            if existing[0] != intent[3]:
                raise ValueError(_ERROR)
            return None
        row = _valid(dict(attempt_id=attempt, thread_id=tid, failure_turn=intent[0], generation=intent[1],
                          policy_generation=intent[3], queue_id=None, state='sending', created_at=timestamp,
                          attempt_state=original[3], resume_turn=original[4]))
        if not _authorised(db, row):
            raise ValueError(_ERROR)
        db.execute('INSERT INTO queue_dispatches (attempt_id,queue_id,policy_generation,state,created_at) VALUES (?,NULL,?,?,?)', (attempt, intent[3], 'sending', timestamp))
        return _public(row)


def pending(db):
    if not _schema(db):
        return []
    if db.execute('SELECT 1 FROM queue_dispatches q LEFT JOIN attempts a ON a.attempt_id=q.attempt_id WHERE a.attempt_id IS NULL LIMIT 1').fetchone():
        raise ValueError(_ERROR)
    rows = _dicts(db.execute(_SELECT + " WHERE q.state NOT IN ('reached','cancelled') ORDER BY q.created_at,q.attempt_id"))
    return [_public(_valid(row)) for row in rows]


def permitted(db, row):
    try:
        current = _lookup(db, row)
        return current['state'] == 'sending' and current['queue_id'] is None and _authorised(db, current)
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        return False


def accepted(db, row, qid):
    qid = core.check_id(qid)
    with _write(db):
        current = _lookup(db, row)
        if current['queue_id'] not in (None, qid):
            raise ValueError(_ERROR)
        if current['state'] in ('reached', 'cancelled'):
            if current['queue_id'] != qid:
                raise ValueError(_ERROR)
            return False
        target = 'queued' if current['state'] == 'sending' else current['state']
        db.execute('UPDATE queue_dispatches SET queue_id=?,state=? WHERE attempt_id=?', (qid, target, current['attempt_id']))
        return True


def _block(db, row):
    db.execute("UPDATE threads SET state='blocked',enabled=0,last_error='送信状態の確認が必要です。自動で再送しません' WHERE id=?", (row['thread_id'],))


def uncertain(db, row):
    with _write(db):
        current = _lookup(db, row)
        if current['state'] in ('reached', 'cancelled'):
            return False
        db.execute("UPDATE queue_dispatches SET state='unknown' WHERE attempt_id=?", (current['attempt_id'],))
        _block(db, current)
        return True


def reached(db, row, turn):
    turn = core.check_id(turn)
    with _write(db):
        current = _lookup(db, row)
        if turn in (current['queue_id'], current['failure_turn']):
            raise ValueError(_ERROR)
        if current['resume_turn'] is not None:
            if current['resume_turn'] != turn or current['state'] != 'reached':
                raise ValueError(_ERROR)
            return False
        if not _authorised(db, current) or current['state'] in ('unknown', 'cancelled'):
            _block(db, current)
        intent = (current['failure_turn'], current['generation'], current['attempt_id'], current['policy_generation'])
        core.record_receipt(db, current['thread_id'], intent, turn)
        saved = db.execute('SELECT state,resume_turn FROM attempts WHERE attempt_id=?', (current['attempt_id'],)).fetchone()
        if saved is None or tuple(saved) != ('received', turn):
            raise ValueError(_ERROR)
        db.execute("UPDATE queue_dispatches SET state='reached' WHERE attempt_id=?", (current['attempt_id'],))
        return True


def cancelled(db, row):
    with _write(db):
        current = _lookup(db, row)
        if current['state'] == 'reached':
            return False
        db.execute("UPDATE queue_dispatches SET state='cancelled' WHERE attempt_id=?", (current['attempt_id'],))
        db.execute("UPDATE attempts SET state='cancelled' WHERE attempt_id=? AND resume_turn IS NULL", (current['attempt_id'],))
        _block(db, current)
        return True
