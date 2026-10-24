"""接続設定は既存の非公開SQLiteへ保存する。認証情報は保存しない。"""
import hashlib
import json
import sys
from cli_security import validate_location

def _fingerprint(executable):
    path = validate_location(executable)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return path, digest.hexdigest()


def enabled(config):
    if isinstance(config, dict) and config.get('mode') == 'managed':
        from desktop_binding import live_binding
        return live_binding(config)
    return (isinstance(config, dict) and config.get('mode') in ('guarded', 'queue') and
            config.get('desktop_confirmed') is True and
            config.get('manual_exclusion_acknowledged') is True)


def validate_executable(config, *, require_confirmation=True):
    if (not isinstance(config, dict) or config.get('mode') not in ('prepared', 'guarded', 'managed', 'queue') or
            (require_confirmation and not enabled(config))):
        raise ValueError('Desktopの接続先と監視中の操作ルールを確認してください')
    path, digest = _fingerprint(config.get('executable', ''))
    if digest != config.get('sha256'):
        raise ValueError('CLIが変更されました。接続設定を確認し直してください')
    return path


def load(db):
    row = db.execute('SELECT value FROM connection_settings WHERE singleton=1').fetchone()
    if not row:
        return {}
    try:
        config = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    if not isinstance(config, dict):
        return {}
    if config.get('mode') == 'managed':
        from desktop_binding import load as session_load, live_binding
        import time
        session = session_load(db)
        config['_runtime_attested'] = live_binding(config)
        if config['_runtime_attested']:
            config['_connection_phase'] = 'ready'
        elif (config.get('manual_exclusion_acknowledged') is not True
              and live_binding(dict(config, manual_exclusion_acknowledged=True))):
            config['_connection_phase'] = 'confirmation_required'
        elif (session.get('status') in ('starting', 'connecting') and
              session.get('nonce') == config.get('launch_nonce') and
              0 <= time.time() - session.get('requested_at', 0) < (300 if sys.platform == 'win32' else 90)):
            config['_connection_phase'] = 'connecting'
        else:
            config['_connection_phase'] = 'failed'
            config['_connection_error'] = 'connection_not_verified'
    return config


def acknowledge_managed(db):
    from desktop_binding import live_binding
    db.execute('BEGIN IMMEDIATE')
    try:
        row = db.execute('SELECT value FROM connection_settings WHERE singleton=1').fetchone()
        config = json.loads(row[0]) if row else {}
        if not isinstance(config, dict) or config.get('mode') != 'managed':
            raise ValueError('接続済みDesktopを確認してください')
        candidate = dict(config, manual_exclusion_acknowledged=True)
        if not live_binding(candidate):
            raise ValueError('接続済みDesktopを確認してください')
        db.execute('UPDATE connection_settings SET value=? WHERE singleton=1 AND value=?',
                   (json.dumps(candidate), row[0]))
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def configure(db, executable, *, desktop_confirmed, manual_exclusion_acknowledged):
    if desktop_confirmed is not True or manual_exclusion_acknowledged is not True:
        raise ValueError('接続先と操作ルールの両方の確認が必要です')
    path, digest = _fingerprint(executable)
    _store(db, dict(mode='guarded', executable=str(path), sha256=digest,
                    desktop_confirmed=True, manual_exclusion_acknowledged=True))


def configure_queue(db, config, *, manual_exclusion_acknowledged, before_store=None):
    if not isinstance(config, dict) or config.get('mode') != 'queue' or manual_exclusion_acknowledged is not True:
        raise ValueError('接続先と監視中の操作ルールを確認してください')
    from queue_bridge import QueueBridge
    candidate = {key: config.get(key) for key in ('mode', 'executable', 'sha256', 'codex_home')}
    candidate.update(desktop_confirmed=True, manual_exclusion_acknowledged=True)
    with QueueBridge(config=candidate) as bridge:
        capabilities = bridge.capabilities()
        if capabilities.get('desktop_binding') is not True or capabilities.get('guarded_admission') is not True:
            raise ValueError('起動中のCodexとの対応を確認できません')
    _store(db, candidate, before_store=before_store)


def _prepared_config(executable):
    path, digest = _fingerprint(executable)
    return dict(mode='prepared', executable=str(path), sha256=digest,
                desktop_confirmed=False, manual_exclusion_acknowledged=False)


def prepare(db, executable):
    """接続の準備だけを保存。再開送信の確認とは分ける。"""
    _store(db, _prepared_config(executable))


def _store_in_transaction(db, config):
    """設定と接続要求を一緒に確定する呼出元も、同じ監視失効処理を使う。"""
    if not db.in_transaction:
        raise RuntimeError('接続設定の書込みtransactionが必要です')
    from queue_state import pending
    if pending(db):
        raise ValueError('送信状態の確認が必要なため接続設定を保持します。自動再開をOFFにして到達状況を確認してください')
    db.execute('INSERT OR REPLACE INTO connection_settings VALUES (1,?)', (json.dumps(config),))
    # Old relays and managers must lose their nonce in the same commit. A new
    # managed request writes its fresh session afterward in this transaction.
    db.execute('DELETE FROM desktop_sessions WHERE singleton=1')
    # A different connection must never inherit already enabled targets.
    db.execute("""UPDATE threads SET enabled=0,state='blocked',generation=generation+1,
               last_error='接続設定が変更されました。監視対象を選び直してください'
               WHERE enabled=1 OR state IN ('claimed','monitoring')""")


def _store(db, config, *, before_store=None):
    db.execute('BEGIN IMMEDIATE')
    try:
        if before_store is not None and before_store() is not True:
            raise ValueError('接続設定の保存前に状態が変わりました')
        _store_in_transaction(db, config)
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
