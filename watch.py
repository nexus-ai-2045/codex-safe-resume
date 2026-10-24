"""選択した既存Desktopチャットだけを照合する安全な監視。"""
import time
import auto_watch
from core import (connection, discover, sync, list_threads, arm_detected, ready,
                  mark, quota, observe_resume_quota, monitor_lease, record_intent, record_receipt)
from desktop_bridge import DesktopBridge, BridgeError
from queue_bridge import QueueNotStarted
from connection_settings import load as connection_options


def _bridge(db, factory, *, for_pending=False):
    config = connection_options(db)
    if factory is DesktopBridge and config.get('mode') == 'queue':
        from queue_bridge import QueueBridge
        return QueueBridge(config=config, for_pending=for_pending)
    return DesktopBridge(config=config) if factory is DesktopBridge else factory()


def block(db, tid, reason):
    db.execute("UPDATE threads SET state='blocked',enabled=0,last_error=? WHERE id=?", (reason, tid))


def reconcile_monitoring(db, threads):
    """保存した受領ターンと一致する観測だけを採用する。"""
    observed = {item['id']: item for item in threads}
    rows = db.execute("SELECT id,resume_turn FROM threads WHERE state='monitoring'").fetchall()
    for tid, receipt in rows:
        current = observed.get(tid)
        if not receipt or not current or not current.get('observation_verified'):
            block(db, tid, '再開ターンを確認できません')
        elif current.get('latest_turn') != receipt:
            block(db, tid, '別のターンへの変更を検出しました')
        elif current.get('awaiting'):
            block(db, tid, '承認または入力を待っています')
        elif current.get('turn_status') == 'completed':
            mark(db, tid, 'completed', automatic=True)
        elif current.get('turn_status') == 'inProgress':
            continue
        elif current.get('turn_status') == 'failed' and current.get('limited'):
            db.execute("UPDATE threads SET state='idle',reset_at=NULL,failure_turn=NULL,resume_turn=NULL WHERE id=?", (tid,))
        else:
            block(db, tid, '終了状態の確認が必要です')


def _matches(snapshot, tid, failure):
    return (isinstance(snapshot, dict) and snapshot.get('id') == tid
            and snapshot.get('observation_verified') is True
            and snapshot.get('latest_turn') == failure
            and snapshot.get('failure_turn') == failure
            and snapshot.get('turn_status') == 'failed'
            and snapshot.get('limited') is True and snapshot.get('awaiting') is False)


def _once(db, bridge_factory):
    import queue_state
    queue_mode = connection_options(db).get('mode') == 'queue'
    if queue_mode:
        queue_state.initialise(db)
    pending = queue_state.pending(db)
    if pending:
        try:
            from queue_reconcile import reconcile
            with _bridge(db, bridge_factory, for_pending=True) as bridge:
                reconcile(db, bridge)
        except Exception:
            for row in pending:
                queue_state.uncertain(db, row)
    policy = auto_watch.status(db)
    if policy is not None and not policy['enabled']:
        return {'state': 'disabled'}
    # Lock ownership proves no other monitor is running; stale claims still never retry.
    protected = {row['thread_id'] for row in queue_state.pending(db)}
    for tid, in db.execute("SELECT id FROM threads WHERE state IN ('claimed','dispatched')").fetchall():
        if tid not in protected:
            block(db, tid, '送信状態を確認してください')
    include_ids = tuple(row[0] for row in db.execute("SELECT id FROM threads WHERE enabled=1 OR state='monitoring'"))
    threads, warning = (discover(limit=None, include_ids=include_ids) if policy is not None
                        else discover(include_ids=include_ids))
    if warning:
        return {'state': 'unavailable'}
    sync(db, threads)
    observed_ids = {thread['id'] for thread in threads}
    missing = []
    for row in list_threads(db):
        if (row['enabled'] or row['state'] == 'monitoring') and row['id'] not in observed_ids:
            # include_ids was explicitly checked. Missing metadata is unknown,
            # never proof of archive or permission to use a stale saved target.
            block(db, row['id'], '現在の一覧で対象を確認できないため監視を停止しました')
            missing.append((row['id'], 'blocked'))
    if missing:
        return {'state': 'checked', 'armed': 0, 'results': missing}
    if not threads:
        return {'state': 'empty'}
    try:
        with _bridge(db, bridge_factory) as bridge:
            caps = bridge.capabilities()
            if caps.get('desktop_binding') is not True:
                return {'state': 'unsupported-transport'}
            selected = [row for row in list_threads(db) if row['enabled'] or row['state'] == 'monitoring']
            candidates = {item['id']: item for item in threads
                          if policy is not None and auto_watch.eligible(db, item)}
            selected_ids = {row['id'] for row in selected}
            selected.extend(row for row in list_threads(db)
                            if row['id'] in candidates and row['id'] not in selected_ids)
            live = []
            for row in selected:
                try:
                    prepare = getattr(bridge, 'prepare_thread', None)
                    if prepare is not None:
                        prepare(row['id'])
                    snapshot = bridge.snapshot(row['id'])
                    if not isinstance(snapshot, dict) or snapshot.get('id') != row['id'] or snapshot.get('observation_verified') is not True:
                        raise BridgeError('観測結果を確認できません')
                    candidate = candidates.get(row['id'])
                    if candidate is not None:
                        if not _matches(snapshot, row['id'], candidate['failure_turn']):
                            continue
                        if not auto_watch.enrol(db, candidate, policy):
                            continue
                    live.append(snapshot)
                except Exception:
                    block(db, row['id'], '現在のチャットを確認できません')
            reconcile_monitoring(db, live)
            reply = bridge.quota()
            available, _ = observe_resume_quota(reply)
            armed = arm_detected(db, live, reply)
            if not available:
                return {'state': 'quota-wait', 'armed': armed}
            if (not (caps.get('safe_admission') is True or caps.get('guarded_admission') is True)
                    or caps.get('desktop_binding') is not True):
                return {'state': 'unsupported-transport'}
            # A newly quota-failed receipt can be retried once if both current quotas
            # already recovered before this tick. No elapsed-time assumption is used.
            for snapshot in live:
                row = db.execute('SELECT state,enabled FROM threads WHERE id=?', (snapshot['id'],)).fetchone()
                if row and row[1] and row[0] == 'idle' and _matches(snapshot, snapshot['id'], snapshot.get('failure_turn')):
                    quota(db, snapshot['id'], snapshot['failure_turn'], int(time.time()))
            claimed = ready(db, live)
            results = []
            for tid in claimed:
                intent = record_intent(db, tid)
                if intent is None:
                    block(db, tid, '同じ停止ターンの試行記録があります')
                    results.append((tid, 'blocked'))
                    continue
                try:
                    latest = bridge.snapshot(tid)
                    if queue_mode:
                        dispatch = queue_state.start(db, tid, intent)
                        if dispatch is None:
                            raise BridgeError('同じ停止ターンのキュー試行を確認してください')
                        if not _matches(latest, tid, intent[0]):
                            queue_state.cancelled(db, dispatch)
                            raise BridgeError('送信前に状態が変わりました')
                        try:
                            result = bridge.send_once(tid, intent[0], attempt_id=intent[2],
                                                      before_send=lambda: queue_state.permitted(db, dispatch))
                        except QueueNotStarted:
                            queue_state.cancelled(db, dispatch)
                            results.append((tid, 'blocked'))
                            continue
                        if isinstance(result, dict) and result.get('delivery') == 'queued':
                            queue_state.accepted(db, dispatch, result['queue']['id'])
                            results.append((tid, 'queued'))
                        elif isinstance(result, dict) and result.get('delivery') == 'not_started':
                            queue_state.cancelled(db, dispatch)
                            results.append((tid, 'blocked'))
                        else:
                            queue_state.uncertain(db, dispatch)
                            results.append((tid, 'blocked'))
                        continue
                    # The intent above is already durable. Hold the selection's
                    # SQLite write boundary through admission and receipt: a
                    # concurrent disable either wins before this transaction,
                    # or takes effect after this already committed send.
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        enabled = db.execute("SELECT enabled,generation FROM threads WHERE id=? AND state='claimed'", (tid,)).fetchone()
                        current_policy = auto_watch.status(db)
                        policy_matches = (current_policy is None if intent[3] is None else
                                          current_policy is not None and current_policy['enabled']
                                          and current_policy['generation'] == intent[3])
                        if (not policy_matches or not _matches(latest, tid, intent[0])
                                or not enabled or not enabled[0] or enabled[1] != intent[1]):
                            raise BridgeError('送信前に状態が変わりました')
                        result = bridge.send_once(tid, intent[0], attempt_id=intent[2])
                        turn = (result.get('turn') or {}).get('id') if isinstance(result, dict) else None
                        record_receipt(db, tid, intent, turn)
                        db.execute('COMMIT')
                    except BaseException:
                        if db.in_transaction:
                            db.execute('ROLLBACK')
                        raise
                    row = db.execute('SELECT state FROM threads WHERE id=?', (tid,)).fetchone()
                    results.append((tid, 'dispatched' if row[0] == 'monitoring' else 'blocked'))
                except Exception:
                    block(db, tid, '送信結果の照合が必要です。自動で再送しません')
                    results.append((tid, 'blocked'))
            return {'state': 'checked', 'armed': armed, 'results': results}
    except Exception:
        return {'state': 'offline'}


def once(db, bridge_factory=DesktopBridge):
    with monitor_lease(db) as acquired:
        if not acquired:
            return {'state': 'busy'}
        return _once(db, bridge_factory)


def main():
    from diagnostics import monitor_status, JapaneseParser
    parser = JapaneseParser(description='Codexの制限解除を安全に監視します')
    parser.add_argument('--once', action='store_true', help='一度だけ確認する')
    parser.add_argument('--interval', type=int, default=30, help='確認間隔（秒、10以上）')
    args = parser.parse_args()
    if args.interval < 10:
        parser.error('確認間隔は10秒以上にしてください')
    db = connection()
    try:
        while True:
            print(monitor_status(once(db)), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    finally:
        db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        import sys
        print('監視を開始できません。状態データと権限を確認してください。', file=sys.stderr)
        raise SystemExit(1)
