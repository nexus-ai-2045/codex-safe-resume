"""Local-only fail-closed Codex Desktop quota resume coordinator (Python 3.10+)."""
from __future__ import annotations
import json
import math
import contextlib
import os
import pathlib
import re
import sqlite3
import stat
import time
import uuid

UUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')


def reject_links(path):
    """保存先と祖先のsymlink、Windowsのreparse pointを拒否する。"""
    path = pathlib.Path(path).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        # macOS ships root-owned /var, /tmp and /etc aliases into /private.
        # Only these exact OS aliases are accepted, never a user-created link.
        system_alias = (os.name == 'posix' and info.st_uid == 0
                        and part.parent == pathlib.Path('/') and part.name in ('var', 'tmp', 'etc')
                        and part.resolve() == pathlib.Path('/private') / part.name)
        if (stat.S_ISLNK(info.st_mode) and not system_alias) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('リンク経由の保存先は使用できません')


def is_quota_error(error):
    """引用やメッセージ中の語句ではなく、構造化されたエラー種別を照合。"""
    if not isinstance(error, dict):
        return False
    for key in ('type', 'code', 'codexErrorInfo'):
        value = error.get(key)
        if value == 'usageLimitExceeded':
            return True
        if key == 'codexErrorInfo' and isinstance(value, dict):
            return set(value) == {'usageLimitExceeded'}
    return False


def quota_bucket(reply, limit_id='codex', *, require_view=False):
    if not isinstance(reply, dict):
        raise ValueError('利用枠の応答が不正です')
    if reply.get('rateLimitsByLimitId') is not None:
        buckets = reply['rateLimitsByLimitId']
        if not isinstance(buckets, dict) or not isinstance(buckets.get(limit_id), dict):
            raise ValueError('対象の利用枠を確認できません')
        bucket = buckets[limit_id]
    else:
        if ((require_view or 'rateLimitsByLimitId' in reply)
                and not isinstance(reply.get('rateLimits'), dict)):
            raise ValueError('公式応答の利用枠を確認できません')
        bucket = reply.get('rateLimits', reply)
    if not isinstance(bucket, dict) or bucket.get('limitId') not in (None, limit_id):
        raise ValueError('利用枠の対応関係を確認できません')
    return bucket


@contextlib.contextmanager
def monitor_lease(db):
    """同じ状態DBを監視するプロセス間の排他。期限切れで送信権を復活させない。"""
    database = db.execute('PRAGMA database_list').fetchone()[2]
    lock_path = pathlib.Path(database).parent / 'monitor.lock'
    reject_links(lock_path)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError('監視ロックが不正です')
        if os.name == 'posix':
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RuntimeError('監視ロックの権限を確認してください')
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        else:
            import msvcrt
            if info.st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired and os.name != 'posix':
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def record_intent(db, tid):
    """送信より先に一意な試行を保存。既存試行は再送しない。"""
    db.execute('BEGIN IMMEDIATE')
    try:
        from auto_watch import status
        policy = status(db)
        row = db.execute("SELECT failure_turn,generation FROM threads WHERE id=? AND enabled=1 AND state='claimed'", (tid,)).fetchone()
        if row is None or (policy is not None and not policy['enabled']):
            db.execute('ROLLBACK')
            return None
        attempt = str(uuid.uuid4())
        db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,NULL)', (tid, row[0], attempt, row[1], 'sending'))
        db.execute('COMMIT')
        return row[0], row[1], attempt, policy['generation'] if policy is not None else None
    except sqlite3.IntegrityError:
        db.execute('ROLLBACK')
        return None
    except BaseException:
        db.execute('ROLLBACK')
        raise


def record_receipt(db, tid, intent, turn):
    if not isinstance(turn, str) or not turn or len(turn) > 128 or turn == intent[0]:
        raise ValueError('再開ターンの受領証を確認できません')
    owns_transaction = not db.in_transaction
    if owns_transaction:
        db.execute('BEGIN IMMEDIATE')
    try:
        db.execute("UPDATE attempts SET state='received',resume_turn=? WHERE attempt_id=?", (turn, intent[2]))
        changed = db.execute("UPDATE threads SET state='monitoring',resume_turn=?,last_error='' WHERE id=? AND enabled=1 AND state='claimed' AND generation=?", (turn, tid, intent[1])).rowcount
        if not changed:
            db.execute("UPDATE threads SET state='blocked',enabled=0,last_error='選択状態が変更されました。確認が必要です' WHERE id=?", (tid,))
        if owns_transaction:
            db.execute('COMMIT')
    except BaseException:
        if owns_transaction:
            db.execute('ROLLBACK')
        raise
SCHEMA = '''CREATE TABLE IF NOT EXISTS threads (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'idle', reset_at INTEGER, failure_turn TEXT, claimed_at INTEGER,
 last_error TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0
)'''


def check_id(s):
    if not isinstance(s, str) or not UUID_PATTERN.fullmatch(s):
        raise ValueError('チャットIDが不正です')
    return str(uuid.UUID(s))


def safe_state_dir(home=None, *, create=True):
    p = pathlib.Path(home or os.environ.get('CODEX_RESUME_HOME', pathlib.Path.home()/'.codex-safe-resume')).expanduser()
    reject_links(p)
    if create:
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not p.is_dir():
        raise FileNotFoundError('状態フォルダーが見つかりません')
    if os.name == 'posix':
        s=p.stat()
        if s.st_uid != os.getuid() or s.st_mode & 0o077: raise RuntimeError('状態フォルダーの所有者または権限を確認してください')
    return p


def connection(home=None, *, readonly=False):
    folder=safe_state_dir(home, create=not readonly); path=folder/'resume.sqlite3'
    reject_links(path)
    if path.is_symlink(): raise RuntimeError('状態データのリンクは使用できません')
    if path.exists():
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise RuntimeError('状態データのリンクまたは種類を確認してください')
        if os.name == 'posix' and (st.st_uid != os.getuid() or st.st_mode & 0o077):
            raise RuntimeError('状態データの所有者または権限を確認してください')
    target = path.absolute().as_uri() + '?mode=ro' if readonly else path
    db=sqlite3.connect(target, uri=readonly, timeout=5, isolation_level=None)
    if os.name == 'posix' and not readonly: path.chmod(0o600)
    try:
        db.execute('PRAGMA busy_timeout=5000')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > 4:
            raise RuntimeError('状態データの版が新しいため読み込みを停止しました')
        if readonly:
            db.execute('PRAGMA query_only=ON')
            return db
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        db.execute(SCHEMA)
        columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
        for name, kind in [('resume_turn', 'TEXT'), ('generation', 'INTEGER NOT NULL DEFAULT 0'),
                           ('auto_excluded', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                db.execute(f'ALTER TABLE threads ADD COLUMN {name} {kind}')
        db.execute('''CREATE TABLE IF NOT EXISTS attempts (
            thread_id TEXT NOT NULL, failure_turn TEXT NOT NULL,
            attempt_id TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL,
            state TEXT NOT NULL, resume_turn TEXT,
            PRIMARY KEY(thread_id, failure_turn))''')
        db.execute('''CREATE TABLE IF NOT EXISTS connection_settings (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS desktop_sessions (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS auto_watch_policy (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
            generation INTEGER NOT NULL CHECK(generation>=0))''')
        if version < 4:
            db.execute("UPDATE threads SET auto_excluded=1 WHERE state IN ('blocked','completed')")
        db.execute('PRAGMA user_version=4')
        db.execute('COMMIT')
    except BaseException:
        db.close()
        raise
    return db


def discover(codex_home=None, limit=400, *, include_ids=()):
    """正本メタデータを読み、保存済み対象は最近の候補数と独立して照合する。"""
    if codex_home is None:
        codex_home = os.environ.get('CODEX_HOME')
    base = (pathlib.Path(codex_home) if codex_home is not None else pathlib.Path.home()/'.codex').expanduser()
    state = base/'state_5.sqlite'
    history = base/'thread_history_1.sqlite'
    if not state.is_file():
        return [], 'Codexの会話データが見つかりません'

    def text(value, length=200):
        return ' '.join(str(value or '').split())[:length]

    def generic(value):
        return not value or value.lower() in ('(untitled)', 'untitled', 'new thread', 'new chat', 'continue') or value in ('続けて', '继续')

    def excluded(row):
        if row['archived'] == 1:
            return 'archived'
        source = row['source']
        if isinstance(source, str):
            if len(source) > 65536:
                raise ValueError('元データの分類を確認できません')
            try:
                parsed = json.loads(source)
            except ValueError:
                parsed = source
            if isinstance(parsed, dict):
                for key in ('subagent', 'subAgent'):
                    value = parsed.get(key)
                    if isinstance(value, dict) or isinstance(value, str) and value in ('review', 'compact', 'memory_consolidation'):
                        return 'subagent'
                if set(parsed) == {'custom'} and parsed['custom'] in ('atlas', 'chatgpt'):
                    parsed = parsed['custom']
            # Official interactive defaults: Cli, VSCode, Custom(atlas/chatgpt).
            if source and (not isinstance(parsed, str) or parsed not in ('cli', 'vscode', 'atlas', 'chatgpt')):
                return 'noninteractive'
        if 'preview' in columns and not text(row['preview']):
            return 'empty'
        return None

    try:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError('候補数を確認できません')
        limit = min(limit, 2000) if limit is not None else None
        requested = tuple(dict.fromkeys(check_id(ident) for ident in include_ids))
        rows, seen = [], set()
        with contextlib.closing(sqlite3.connect(state.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA busy_timeout=1000')
            columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
            if not {'id', 'title', 'cwd'} <= columns:
                return [], 'Codexの会話データ形式に対応していません'
            fields = ['id', 'substr(title,1,200) AS title', 'substr(cwd,1,400) AS cwd']
            for name, length in (('name', 200), ('source', 65537), ('first_user_message', 4096), ('preview', 200)):
                fields.append(f'substr({name},1,{length}) AS {name}' if name in columns else f'NULL AS {name}')
            for name in ('archived', 'updated_at_ms', 'updated_at'):
                fields.append(name if name in columns else f'NULL AS {name}')
            projection = ','.join(fields)
            chronology = []
            if 'updated_at_ms' in columns:
                chronology.append('updated_at_ms')
            if 'updated_at' in columns:
                chronology.append('updated_at*1000')
            order = ('COALESCE('+','.join(chronology)+')' if len(chronology) > 1 else chronology[0]) if chronology else 'rowid'
            db.row_factory = sqlite3.Row

            def metadata(sql, parameters=()):
                cursor = db.execute(sql, parameters)
                try:
                    while batch := cursor.fetchmany(128):
                        for record in batch:
                            row = dict(record)
                            if not isinstance(row['id'], str) or not UUID_PATTERN.fullmatch(row['id']):
                                continue
                            row['excluded_reason'] = excluded(row)
                            yield row
                finally:
                    cursor.close()

            if limit is None or limit:
                filters = ['archived IS NOT 1'] if 'archived' in columns else []
                if 'preview' in columns:
                    filters.append("COALESCE(preview,'')<>''")
                archive_filter = 'WHERE ' + ' AND '.join(filters) if filters else ''
                candidates = metadata(f'SELECT {projection} FROM threads {archive_filter} ORDER BY {order} DESC,rowid DESC')
                try:
                    for row in candidates:
                        if row['excluded_reason']:
                            continue
                        rows.append(row)
                        seen.add(check_id(row['id']))
                        if limit is not None and len(rows) >= limit:
                            break
                finally:
                    candidates.close()
            for offset in range(0, len(requested), 250):
                group = requested[offset:offset+250]
                marks = ','.join('?' for _ in group)
                for row in metadata(f'SELECT {projection} FROM threads WHERE id IN ({marks})', group):
                    ident = check_id(row['id'])
                    if ident not in seen:
                        rows.append(row)
                        seen.add(ident)

        latest, first_messages = {}, {}
        normal = [row for row in rows if not row['excluded_reason']]
        ids = [row['id'] for row in normal]
        fallback_ids = [row['id'] for row in normal if not text(row['name'])
                        and generic(text(row['title'])) and not text(row['first_user_message'], 180)
                        and not text(row['preview'], 180)]
        if ids and history.is_file():
            with contextlib.closing(sqlite3.connect(history.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('PRAGMA busy_timeout=1000')
                columns = {row[1] for row in db.execute('PRAGMA table_info(thread_turns)')}
                if {'thread_id', 'turn_id', 'status', 'error_json'} <= columns:
                    for offset in range(0, len(ids), 250):
                        group = ids[offset:offset+250]
                        marks = ','.join('?' for _ in group)
                        query = f"""SELECT t.thread_id,t.turn_id,t.status,substr(t.error_json,1,65536)
                                    FROM thread_turns AS t WHERE t.rowid IN (
                                    SELECT MAX(rowid) FROM thread_turns WHERE thread_id IN ({marks}) GROUP BY thread_id)"""
                        for ident, turn, status, error in db.execute(query, group):
                            latest[ident] = (str(turn), str(status), str(error or ''))
                columns = {row[1] for row in db.execute('PRAGMA table_info(thread_items)')}
                if {'thread_id', 'item_type', 'item_json', 'rollout_ordinal'} <= columns:
                    for offset in range(0, len(fallback_ids), 150):
                        group = fallback_ids[offset:offset+150]
                        marks = ','.join('?' for _ in group)
                        query = f"""SELECT t.thread_id,substr(t.item_json,1,65536) FROM thread_items AS t
                                    WHERE t.thread_id IN ({marks}) AND t.rowid=(SELECT rowid FROM thread_items
                                    WHERE thread_id=t.thread_id AND item_type='userMessage'
                                    ORDER BY rollout_ordinal ASC,rowid ASC LIMIT 1)"""
                        for ident, raw in db.execute(query, group):
                            try:
                                chunks = json.loads(raw).get('content', [])
                                message = ' '.join(chunk.get('text', '') for chunk in chunks
                                                   if isinstance(chunk, dict) and isinstance(chunk.get('text'), str))
                                first_messages[ident] = text(message, 180)
                            except (ValueError, TypeError, AttributeError):
                                pass
        result = []
        for row in rows:
            ident = row['id']
            recent = latest.get(ident)
            limited = False
            if recent and recent[1] == 'failed':
                try:
                    limited = is_quota_error(json.loads(recent[2]))
                except (ValueError, TypeError):
                    pass
            name, title = text(row['name']), text(row['title'])
            if name:
                title = name
            elif generic(title):
                title = text(row['first_user_message'], 180) or text(row['preview'], 180) or first_messages.get(ident) or title or '(タイトルなし)'
            record = dict(id=check_id(ident), title=title, cwd=str(row['cwd'] or '')[:400], limited=limited,
                          failure_turn=recent[0] if limited else None, latest_turn=recent[0] if recent else None,
                          turn_status=recent[1] if recent else 'unknown', observation_verified=False)
            if row['excluded_reason']:
                record['excluded_reason'] = row['excluded_reason']
            for field, divisor in (('updated_at_ms', 1000), ('updated_at', 1)):
                value = row[field]
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    record['source_updated_at'] = value / divisor
                    break
            result.append(record)
        return result, None
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return [], 'Codexの会話データを読み取れません'


def sync(db, discovered):
    now=int(time.time())
    for t in discovered:
        tid=check_id(t['id'])
        db.execute('INSERT INTO threads(id,title,cwd,updated_at) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,cwd=excluded.cwd,updated_at=excluded.updated_at',(tid,t['title'],t['cwd'],now))
        if t.get('excluded_reason') in ('archived', 'subagent', 'noninteractive', 'empty'):
            reason = {'archived': 'アーカイブ済みのため監視を解除しました',
                      'subagent': '子エージェントのため監視を解除しました',
                      'noninteractive': '通常のチャット一覧の対象外のため監視を解除しました',
                      'empty': '公式一覧のプレビュー条件を満たさないため監視を解除しました'}[t['excluded_reason']]
            db.execute("""UPDATE threads SET generation=generation+CASE
                       WHEN enabled!=0 OR state!='blocked' THEN 1 ELSE 0 END,
                       state='blocked',enabled=0,reset_at=NULL,last_error=? WHERE id=?""", (reason, tid))
            continue
        row=db.execute('SELECT state,failure_turn FROM threads WHERE id=?',(tid,)).fetchone()
        if row and row[0] in ('waiting','ready') and not t['limited']:
            # Any later normal/unknown turn invalidates authorization for unattended restart.
            db.execute("UPDATE threads SET state='blocked',enabled=0,last_error='最新ターンが変わりました。確認が必要です' WHERE id=?",(tid,))
        if row and row[0] in ('waiting','ready') and row[1] and row[1]!=t['failure_turn']:
            db.execute("UPDATE threads SET state='blocked',enabled=0,last_error='制限停止したターンが変わりました。確認が必要です' WHERE id=?",(tid,))
    return len(discovered)


def list_threads(db):
    db.row_factory=sqlite3.Row
    return [dict(x) for x in db.execute('SELECT * FROM threads ORDER BY updated_at DESC, title')]


def enable(db,thread_id,flag):
    thread_id=check_id(thread_id)
    row=db.execute('SELECT state FROM threads WHERE id=?',(thread_id,)).fetchone()
    if not row:raise ValueError('チャットが見つかりません')
    if flag and row[0] in ('claimed','dispatched','blocked','completed'):raise ValueError('手動確認が必要です')
    db.execute('UPDATE threads SET enabled=?,auto_excluded=?,generation=generation+1 WHERE id=?',
               (int(flag), int(not flag), thread_id))


def quota(db,thread_id,failure_turn,reset_at,limit='5h'):
    tid=check_id(thread_id)
    if not failure_turn or not isinstance(failure_turn,str) or len(failure_turn)>128:raise ValueError('停止ターンが不正です')
    if limit not in ('5h','weekly'):raise ValueError('利用枠の種類が不正です')
    if not isinstance(reset_at,int) or reset_at < 1700000000 or reset_at > 4102444800:raise ValueError('解除時刻が不正です')
    row=db.execute('SELECT state FROM threads WHERE id=?',(tid,)).fetchone()
    if not row or row[0] in ('completed','blocked','claimed','dispatched'):raise ValueError('停止中のチャットは登録できません')
    db.execute("UPDATE threads SET failure_turn=?,reset_at=?,state='waiting',last_error='' WHERE id=?",(failure_turn,reset_at,tid))


def mark(db,thread_id,status, *, expected_generation=None, automatic=False):
    tid=check_id(thread_id)
    if status not in ('completed','blocked','idle'):raise ValueError('状態が不正です')
    if status == 'idle':
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError('確認した状態の世代が必要です')
        # 人間確認後の選び直しだけ。再選択まで個別除外し、試行履歴は残す。
        changed = db.execute("""UPDATE threads SET state='idle',enabled=0,auto_excluded=1,
                             generation=generation+1,reset_at=NULL,failure_turn=NULL,
                             claimed_at=NULL,resume_turn=NULL,last_error=''
                             WHERE id=? AND enabled=0 AND state IN ('blocked','completed')
                             AND generation=?""",
                             (tid, expected_generation)).rowcount
        if changed != 1:
            raise ValueError('無効な要確認・完了のチャットだけ選び直せます')
        return
    if automatic:
        if status != 'completed':
            raise ValueError('自動完了以外は明示的な確認が必要です')
        changed = db.execute("UPDATE threads SET state='completed',enabled=0,reset_at=NULL WHERE id=? AND state='monitoring'",
                             (tid,)).rowcount
    else:
        changed = db.execute("UPDATE threads SET state=?,enabled=0,auto_excluded=1,reset_at=NULL,failure_turn=NULL WHERE id=?",
                             (status, tid)).rowcount
    if changed != 1:
        raise ValueError('チャットが見つかりません')


def ready(db,discovered,now=None):
    """One-shot atomic claim, no send. Caller must independently verify current server quota."""
    now=int(time.time()) if now is None else int(now); byid={t['id']:t for t in discovered}
    due=db.execute("SELECT id,failure_turn,reset_at FROM threads WHERE enabled=1 AND state='waiting' AND reset_at<=?",(now,)).fetchall()
    accepted=[]
    for tid,failure,reset in due:
        live=byid.get(tid)
        if not live or not live['limited'] or live['failure_turn']!=failure:
            mark(db,tid,'blocked');continue
        db.execute('BEGIN IMMEDIATE')
        try:
            n=db.execute("UPDATE threads SET state='claimed',claimed_at=? WHERE id=? AND enabled=1 AND state='waiting' AND failure_turn=? AND reset_at=?",(now,tid,failure,reset)).rowcount
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK');raise
        if n: accepted.append(tid)
    return accepted


def dry_run(db,discovered,now=None):
    """Preview due threads without mutating claims."""
    now=int(time.time()) if now is None else int(now); byid={t['id']:t for t in discovered}
    return [t['id'] for t in list_threads(db) if t['enabled'] and t['state']=='waiting' and t['reset_at'] and t['reset_at']<=now and t['id'] in byid and byid[t['id']]['limited'] and byid[t['id']]['failure_turn']==t['failure_turn']]

# Remote-control transport is only enabled against the existing desktop/daemon
# control socket. Never spawn a competing server or inject GUI keystrokes.
def control_socket(codex_home=None):
    base=pathlib.Path(codex_home or os.environ.get('CODEX_HOME', pathlib.Path.home()/'.codex')).expanduser()
    return base/'app-server-control'/'app-server-control.sock'


def observe_quota(reset_reply, *, limit_id='codex', require_both=False):
    """Fail closed on malformed/unknown rate-limit data; never treat NaN as available."""
    if not isinstance(reset_reply, dict): raise ValueError('利用枠の応答が不正です')
    limits = quota_bucket(reset_reply, limit_id)
    if not isinstance(limits,dict): raise ValueError('利用枠を確認できません')
    # 旧応答での省略だけを互換扱いにする。明示nullは情報なしであり、
    # 使用率や解除時刻からbackendの利用許可を補ってはいけない。
    ordinary_allowed = reset_reply.get('ordinaryUsageAllowed', True)
    spend_reached = limits.get('spendControlReached', False)
    if type(ordinary_allowed) is not bool or type(spend_reached) is not bool:
        raise ValueError('利用許可または支出制限の状態を確認できません')
    reached=limits.get('rateLimitReachedType')
    windows=[limits.get('primary'),limits.get('secondary')]
    if not any(isinstance(window, dict) for window in windows):
        raise ValueError('利用枠を確認できません')
    if require_both and not all(isinstance(window, dict) for window in windows):
        raise ValueError('両方の利用枠を確認できません')
    reset_candidates=[]
    for w in windows:
        if w is None:continue
        if not isinstance(w,dict):raise ValueError('利用枠の形式が不正です')
        value=w.get('usedPercent')
        if isinstance(value,bool) or not isinstance(value,(int,float)):
            raise ValueError('利用率を確認できません')
        percent=float(value)
        if not math.isfinite(percent) or not 0<=percent<=100:
            raise ValueError('利用率が範囲外です')
        if percent>=100:
            raw_reset=w.get('resetsAt')
            if isinstance(raw_reset,bool) or not isinstance(raw_reset,int) or raw_reset<=0:
                raise ValueError('解除時刻を確認できません')
            reset_candidates.append(raw_reset)
    if reset_candidates:return False,max(reset_candidates)
    if not ordinary_allowed or spend_reached:
        return False, None
    if reached not in (None, '', 'none'):
        return False, None
    return True,None


def observe_resume_quota(reply):
    """送信経路の現在値を確認する。週次1窓は明示的なbackend許可が必須。

    旧2窓の契約を維持し、位置ではなく期間で週次を特定する。
    表示専用の応答・キャッシュ・解除時刻から許可を補わない。
    """
    limits = quota_bucket(reply, require_view=True)
    windows = [limits.get('primary'), limits.get('secondary')]
    provided = [window for window in windows if window is not None]
    if len(provided) == 1 and isinstance(provided[0], dict):
        window = provided[0]
        account = reply.get('accountId')
        permission = reply.get('ordinaryUsageAllowed')
        if (not isinstance(account, str) or not account.strip()
                or type(permission) is not bool
                or type(limits.get('spendControlReached')) is not bool
                or type(window.get('windowDurationMins')) is not int
                or window['windowDurationMins'] != 10080):
            raise ValueError('週次枠の利用許可を確認できません')
        # False means wait; True never overrides a full/invalid window or a
        # backend spend-control/limit reason. Generic validation remains shared.
        return observe_quota(reply)
    return observe_quota(reply, require_both=True)


def arm_detected(db, threads, reset_reply, now=None):
    """Save a validated quota pause and the latest reset across all limited windows."""
    now=int(time.time()) if now is None else now
    observe_quota(reset_reply)
    limits=quota_bucket(reset_reply)
    windows=[w for w in (limits.get('primary'),limits.get('secondary')) if isinstance(w,dict)]
    future=[int(w['resetsAt']) for w in windows if w.get('resetsAt') and
            w.get('usedPercent') is not None and float(w['usedPercent']) >= 100 and int(w['resetsAt'])>now]
    if not future: return 0
    reset=max(future)
    count=0
    for item in threads:
        if not item['limited'] or not item['failure_turn']: continue
        row=db.execute('SELECT enabled,state,failure_turn FROM threads WHERE id=?',(item['id'],)).fetchone()
        if not row or not row[0] or row[1] in ('claimed','dispatched','completed','blocked'):continue
        if row[1]=='waiting' and row[2]==item['failure_turn']:
            db.execute('UPDATE threads SET reset_at=? WHERE id=?',(reset,item['id']))
        else:
            quota(db,item['id'],item['failure_turn'],reset,'5h')
        count+=1
    return count
