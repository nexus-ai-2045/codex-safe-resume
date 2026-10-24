"""Conservative auto-resume monitor for an existing Codex desktop control socket."""
import argparse
import time
from core import connection, discover, sync, list_threads, arm_detected, ready, mark
from desktop_bridge import DesktopBridge, BridgeError


def reconcile_monitoring(db, threads):
    """Follow a dispatched turn without treating silence as task completion.

    A different quota-failed turn can begin a new wait cycle. Unknown/non-quota
    outcomes stop for human review, rather than issuing a blind continuation.
    """
    observed = {item['id']: item for item in threads}
    rows = db.execute("SELECT id, failure_turn FROM threads WHERE enabled=1 AND state='monitoring'").fetchall()
    for tid, previous in rows:
        latest = observed.get(tid)
        if latest is None:
            db.execute("UPDATE threads SET state='blocked',enabled=0,last_error='Thread unavailable for reconciliation' WHERE id=?", (tid,))
        elif latest.get('limited') and latest.get('failure_turn') and latest['failure_turn'] != previous:
            db.execute("UPDATE threads SET state='idle',reset_at=NULL,failure_turn=NULL,last_error='' WHERE id=?", (tid,))
        elif latest.get('limited') and latest.get('failure_turn') == previous:
            continue
        else:
            db.execute("UPDATE threads SET state='blocked',enabled=0,last_error='Turn status needs review' WHERE id=?", (tid,))


def once(db,bridge_factory=DesktopBridge):
    threads, warning=discover()
    if warning: return {'state':'unavailable','reason':warning}
    sync(db,threads)
    reconcile_monitoring(db,threads)
    if not threads: return {'state':'empty'}
    try:
        with bridge_factory() as bridge:
            rl=bridge.quota()
            armed=arm_detected(db,threads,rl)
            # Recheck per-thread before sending in bridge.send_once.
            from core import observe_quota
            available,_=observe_quota(rl)
            if not available:return {'state':'quota-wait','armed':armed}
            claimed=ready(db,threads)
            results=[]
            for tid in claimed:
                record=db.execute('SELECT failure_turn FROM threads WHERE id=?',(tid,)).fetchone()
                try:
                    result=bridge.send_once(tid,record[0])
                    if not isinstance(result,dict) or not (result.get('turn') or {}).get('id'):
                        raise BridgeError('No turn receipt; result uncertain')
                    db.execute("UPDATE threads SET state='monitoring',enabled=1,last_error='' WHERE id=? AND state='claimed'",(tid,))
                    results.append((tid,'dispatched'))
                except Exception as exc:
                    # State of partial send may be unknown. Never retry blindly.
                    db.execute("UPDATE threads SET state='blocked',enabled=0,last_error=? WHERE id=? AND state='claimed'",('Manual reconciliation required: '+type(exc).__name__,tid))
                    results.append((tid,'blocked'))
            return {'state':'checked','armed':armed,'results':results}
    except Exception as exc:
        return {'state':'offline','reason':type(exc).__name__}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--interval',type=int,default=30)
    args=parser.parse_args()
    if args.interval<10:parser.error('interval must be >=10 seconds')
    with connection() as db:
        while True:
            print(once(db),flush=True)
            if args.once:break
            time.sleep(args.interval)

if __name__=='__main__':main()