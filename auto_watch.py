"""GUIの全体監視設定。既存の状態機械と試行台帳を保持する。"""
from core import enable


def status(db):
    row = db.execute('SELECT enabled,generation FROM auto_watch_policy WHERE singleton=1').fetchone()
    return {'enabled': bool(row[0]), 'generation': row[1]} if row is not None else None


def _disable_targets(db):
    # OFF is not a manual exclusion. Unknown sends keep their durable latch.
    db.execute("""UPDATE threads SET enabled=0,generation=generation+1,
                  state=CASE WHEN state IN ('claimed','dispatched') THEN 'blocked' ELSE state END,
                  last_error=CASE WHEN state IN ('claimed','dispatched')
                    THEN '送信状態の確認が必要です。自動で再送しません' ELSE last_error END""")


def initialize(db):
    """初回GUI起動のみOFFへ移行。旧CLIでは呼ばずselected動作を保持。"""
    db.execute('BEGIN IMMEDIATE')
    try:
        if status(db) is None:
            db.execute('INSERT INTO auto_watch_policy VALUES (1,0,0)')
            _disable_targets(db)
        result = status(db)
        db.execute('COMMIT')
        return result
    except BaseException:
        db.execute('ROLLBACK')
        raise


def set_enabled(db, enabled, *, before_change=None):
    if type(enabled) is not bool:
        raise ValueError('監視設定はONまたはOFFで指定してください')
    db.execute('BEGIN IMMEDIATE')
    try:
        if before_change is not None and before_change() is not True:
            raise ValueError('監視設定の変更前に状態が変わりました')
        if status(db) is None:
            db.execute('INSERT INTO auto_watch_policy VALUES (1,0,0)')
            _disable_targets(db)
        db.execute('UPDATE auto_watch_policy SET enabled=?,generation=generation+1 WHERE singleton=1',
                   (int(enabled),))
        if not enabled:
            _disable_targets(db)
        else:
            # OFF pauses the received series, but never grants a new send.
            # Live reconciliation and the new policy generation still gate it.
            db.execute("""UPDATE threads SET enabled=1,generation=generation+1
                          WHERE enabled=0 AND auto_excluded=0
                          AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.thread_id=threads.id
                              AND (a.state!='received' OR a.resume_turn IS NULL))
                          AND EXISTS (SELECT 1 FROM attempts a WHERE a.thread_id=threads.id
                              AND a.state='received' AND a.resume_turn IS NOT NULL
                              AND ((threads.state='monitoring'
                                    AND a.failure_turn=threads.failure_turn
                                    AND a.resume_turn=threads.resume_turn)
                                   OR (threads.state='waiting'
                                       AND a.resume_turn=threads.failure_turn
                                       AND NOT EXISTS (SELECT 1 FROM attempts sent
                                           WHERE sent.thread_id=threads.id
                                           AND sent.failure_turn=threads.failure_turn))))""")
        result = status(db)
        db.execute('COMMIT')
        return result
    except BaseException:
        db.execute('ROLLBACK')
        raise


def eligible(db, item):
    """local候補の絞り込み。採用には別途verified snapshotを必要とする。"""
    failure = item.get('failure_turn')
    if (item.get('excluded_reason') or item.get('limited') is not True
            or item.get('turn_status') != 'failed'
            or not isinstance(failure, str) or not failure or len(failure) > 128):
        return False
    row = db.execute('SELECT state,auto_excluded,failure_turn,resume_turn FROM threads WHERE id=?',
                     (item['id'],)).fetchone()
    if row is None or row[1] or row[0] not in ('idle', 'waiting', 'completed'):
        return False
    if db.execute("""SELECT 1 FROM attempts WHERE thread_id=?
                     AND (failure_turn=? OR resume_turn=? OR state!='received' OR resume_turn IS NULL) LIMIT 1""",
                  (item['id'], failure, failure)).fetchone():
        return False
    if row[0] == 'completed':
        if not row[2] or not row[3] or failure in (row[2], row[3]):
            return False
        # Only our observed completion with the exact durable receipt qualifies.
        if not db.execute("""SELECT 1 FROM attempts WHERE thread_id=? AND failure_turn=?
                             AND resume_turn=? AND state='received'""",
                          (item['id'], row[2], row[3])).fetchone():
            return False
    return True


def enrol(db, item, policy):
    """snapshot照合後だけ呼ぶ。全体/個別の同時変更をtransaction内で再確認。"""
    db.execute('BEGIN IMMEDIATE')
    try:
        current = status(db)
        if current != policy or not current or not current['enabled'] or not eligible(db, item):
            db.execute('ROLLBACK')
            return False
        row = db.execute('SELECT state,enabled FROM threads WHERE id=?', (item['id'],)).fetchone()
        if row[0] == 'completed':
            db.execute("""UPDATE threads SET state='idle',reset_at=NULL,claimed_at=NULL,
                          failure_turn=NULL,resume_turn=NULL WHERE id=?""", (item['id'],))
        if not row[1]:
            enable(db, item['id'], True)
        db.execute('COMMIT')
        return True
    except BaseException:
        db.execute('ROLLBACK')
        raise
