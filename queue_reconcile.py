import time

import auto_watch
import queue_state


def reconcile(db, bridge, *, now=None, pending_seconds=30):
    timestamp = time.time() if now is None else now
    results = []
    for row in queue_state.pending(db):
        thread = row['thread_id']
        attempt = row['attempt_id']
        try:
            receipt = bridge.receipt(thread, attempt, queue_id=row['queue_id'])
            if receipt.get('delivery') == 'reached':
                queue_state.reached(db, row, receipt['turn']['id'])
                results.append((thread, 'received'))
                continue
            if row['queue_id'] is None:
                found = bridge.find_pending(thread, attempt)
                if found is not None:
                    queue_state.accepted(db, row, found)
                    row = next(item for item in queue_state.pending(db) if item['attempt_id'] == attempt)
            policy = auto_watch.status(db)
            selected = db.execute('SELECT enabled,generation FROM threads WHERE id=?', (thread,)).fetchone()
            authorised = (selected is not None and selected[0] and selected[1] == row['generation']
                          and (policy is None if row['policy_generation'] is None else
                               policy is not None and policy['enabled'] and policy['generation'] == row['policy_generation']))
            elapsed = timestamp - row['created_at']
            if (not authorised or row['state'] == 'unknown' or receipt.get('delivery') != 'pending'
                    or not 0 <= elapsed <= pending_seconds):
                if row['queue_id'] is not None:
                    bridge.cancel_pending(thread, attempt, row['queue_id'])
                    after = bridge.receipt(thread, attempt, queue_id=row['queue_id'])
                    if after.get('delivery') == 'reached':
                        queue_state.reached(db, row, after['turn']['id'])
                        results.append((thread, 'received'))
                        continue
                queue_state.uncertain(db, row)
                results.append((thread, 'blocked'))
            else:
                results.append((thread, 'queued'))
        except Exception:
            queue_state.uncertain(db, row)
            results.append((thread, 'blocked'))
    return results
