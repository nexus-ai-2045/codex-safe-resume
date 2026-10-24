"""実中継の最小証明。RPC本文を保存せず、再起動後も生存状態を再照合する。"""
from contextlib import closing
import ctypes
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import threading
import time

from core import connection, control_socket, reject_links, safe_state_dir

LEASE_SECONDS = 8
BUSY_RETRY_SECONDS = .1


def _busy(error):
    code = getattr(error, 'sqlite_errorcode', None)
    return isinstance(code, int) and code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _session_writer(home):
    """既存coreで検証した既存DBだけを、migrationとwriter待機なしで開く。"""
    path = (safe_state_dir(home, create=False) / 'resume.sqlite3').resolve()
    before = path.stat()
    with closing(connection(home, readonly=True)) as reader:
        if Path(reader.execute('PRAGMA database_list').fetchone()[2]).resolve() != path:
            raise ValueError('状態データの保存先が変わりました')
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True,
                             timeout=0, isolation_level=None)
        try:
            reject_links(path)
            after = path.stat()
            if ((before.st_dev, before.st_ino, before.st_uid, before.st_mode, before.st_nlink)
                    != (after.st_dev, after.st_ino, after.st_uid, after.st_mode, after.st_nlink)
                    or Path(db.execute('PRAGMA database_list').fetchone()[2]).resolve() != path):
                raise ValueError('状態データの実体が変わりました')
            return db
        except BaseException:
            db.close()
            raise


def load(db):
    row = db.execute('SELECT value FROM desktop_sessions WHERE singleton=1').fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def store(db, value):
    db.execute('INSERT OR REPLACE INTO desktop_sessions VALUES (1,?)', (json.dumps(value),))


def update(db, nonce, **changes):
    db.execute('BEGIN IMMEDIATE')
    try:
        current = load(db)
        if current.get('nonce') != nonce:
            db.execute('ROLLBACK')
            return False
        current.update(changes)
        store(db, current)
        db.execute('COMMIT')
        return True
    except BaseException:
        db.execute('ROLLBACK')
        raise


def process_identity(pid):
    """Darwin libprocの開始時刻（microsecond）と実行パス。未知OSは拒否。"""
    if sys.platform == 'win32':
        from windows_platform import process_identity as windows_identity
        return windows_identity(pid)
    if sys.platform != 'darwin' or type(pid) is not int or pid <= 0:
        raise ValueError('プロセスの同一性を確認できません')
    class BSDInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in (
            'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
            'rgid', 'svuid', 'svgid', 'reserved')]
        _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
        _fields_ += [(name, ctypes.c_uint32) for name in (
            'nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid', 'nice')]
        _fields_ += [('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    info = BSDInfo()
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
        raise ValueError('プロセスの開始時刻を確認できません')
    path = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, path, len(path)) <= 0 or info.uid != os.getuid():
        raise ValueError('プロセスの所有者を確認できません')
    return dict(pid=pid, ppid=info.ppid, start=[info.start_sec, info.start_usec],
                executable=path.value.decode('utf-8'))


def desktop_bundle(pid):
    if sys.platform == 'win32':
        from windows_platform import desktop_bundle as windows_bundle
        return windows_bundle(pid)
    if sys.platform != 'darwin':
        raise ValueError('Desktopのbundleを確認できません')
    import AppKit
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None or app.isTerminated():
        raise ValueError('Desktopのbundleを確認できません')
    return str(app.bundleIdentifier())


def endpoint_identity(path):
    if sys.platform == 'win32':
        from windows_platform import endpoint_identity as windows_endpoint
        return windows_endpoint(path)
    path = Path(path)
    reject_links(path)
    info = path.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('共有接続の所有者または権限を確認できません')
    return dict(device=info.st_dev, inode=info.st_ino, epoch=info.st_ctime_ns)


def _same_process(expected):
    if not isinstance(expected, dict):
        return False
    actual = process_identity(expected['pid'])
    return all(actual[key] == expected[key] for key in ('pid', 'start', 'executable'))


def _descendant(child, parent):
    current = process_identity(child['pid'])
    seen = set()
    for _ in range(16):
        if all(current[key] == parent[key] for key in ('pid', 'start', 'executable')):
            return True
        pid = current['ppid']
        if pid <= 1 or pid in seen:
            return False
        seen.add(pid)
        current = process_identity(pid)
    return False


def validate_startup_caller(config):
    from connection_settings import load as load_settings, validate_executable
    if (config.get('mode') != 'managed' or not config.get('launch_nonce')
            or os.environ.get('CSR_DESKTOP_NONCE') != config['launch_nonce']):
        raise ValueError('Desktopの起動要求を確認できません')
    validate_executable(config, require_confirmation=False)
    identity = process_identity(os.getpid())
    deadline = time.monotonic() + LEASE_SECONDS
    while True:
        with closing(connection(config['session_home'], readonly=True)) as db:
            state = load(db)
            current_config = load_settings(db)
        runtime_fields = {'_runtime_attested', '_connection_phase', '_connection_error'}
        if ({key: value for key, value in config.items() if key not in runtime_fields}
                != {key: value for key, value in current_config.items() if key not in runtime_fields}
                or state.get('nonce') != config['launch_nonce']
                or state.get('status') not in ('starting', 'connecting', 'disconnected')
                or state.get('sha256') != config.get('sha256')
                or state.get('bridge') not in (None, identity)
                or not 0 <= time.time() - state['requested_at'] <= 300):
            raise ValueError('Desktopの起動要求が変更されました')
        if state.get('status') == 'disconnected' and state.get('bridge') != identity:
            raise ValueError('Desktopの起動要求が失効しています')
        if 'desktop' in state:
            break
        if time.monotonic() >= deadline:
            raise ValueError('Desktopの起動元を確認できません')
        time.sleep(.05)
    expected = state['desktop']
    if (not _same_process(expected) or not _same_process(state['manager'])
            or desktop_bundle(expected['pid']) != state['bundle_id']
            or os.path.normcase(expected['executable']) != os.path.normcase(state['desktop_executable'])
            or os.path.normcase(state['manager']['executable']) != os.path.normcase(state['bridge_executable'])):
        raise ValueError('Desktopの起動元が変わりました')
    current = identity
    for _ in range(2):
        if os.path.normcase(current['executable']) != os.path.normcase(state['bridge_executable']):
            break
        parent = process_identity(current['ppid'])
        current_start, parent_start = current['start'], parent['start']
        if sys.platform == 'win32':
            current_start, parent_start = list(reversed(current_start)), list(reversed(parent_start))
        if tuple(current_start) < tuple(parent_start):
            break
        if parent == expected:
            return
        current = parent
    raise ValueError('Desktopの接続用プロセスを確認できません')


def record_startup_fallback(config, reason):
    from connection_settings import load as load_settings
    allowed = {'unsupported_launch_options', 'shared_transport_unavailable',
               'shared_settings_mismatch', 'receipt_unavailable'}
    if reason not in allowed:
        raise ValueError('接続失敗の分類を確認できません')
    identity = process_identity(os.getpid())
    with closing(_session_writer(config['session_home'])) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            state = load(db)
            current_config = load_settings(db)
            runtime_fields = {'_runtime_attested', '_connection_phase', '_connection_error'}
            if (state.get('nonce') != config.get('launch_nonce')
                    or {key: value for key, value in config.items() if key not in runtime_fields}
                    != {key: value for key, value in current_config.items() if key not in runtime_fields}
                    or state.get('status') not in ('starting', 'connecting', 'disconnected')
                    or state.get('bridge') not in (None, identity)
                    or (state.get('status') == 'disconnected'
                        and state.get('bridge') != identity)):
                raise ValueError('Desktopの起動要求が変更されました')
            state.update(status='fallback', heartbeat=0, startup_fallback_reason=reason,
                         initialize_delivered=False, initialized_forwarded=False)
            store(db, state)
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def live_binding(config, endpoint=None, *, now=None):
    """保存receipt単体でtrueにしない。毎回CLI・PID・祖先・socketを再照合。"""
    try:
        if config.get('mode') != 'managed' or config.get('manual_exclusion_acknowledged') is not True:
            return False
        from connection_settings import validate_executable
        validate_executable(config, require_confirmation=False)
        with closing(connection(config['session_home'], readonly=True)) as db:
            receipt = load(db)
        moment = time.time() if now is None else now
        age = moment - receipt['heartbeat']
        if (receipt.get('status') != 'bound' or receipt.get('nonce') != config.get('launch_nonce')
                or receipt.get('sha256') != config.get('sha256')
                or not 0 <= age <= LEASE_SECONDS
                or receipt.get('initialize_delivered') is not True
                or not (receipt.get('initialized_forwarded') is True
                        or (sys.platform == 'win32'
                            and config.get('initialization_protocol') == 'post_initialize_response'
                            and receipt.get('initialization_evidence') == 'post_initialize_response'))):
            return False
        if endpoint_identity(endpoint or control_socket()) != receipt['endpoint']:
            return False
        if desktop_bundle(receipt['desktop']['pid']) != receipt['bundle_id']:
            return False
        if not all(_same_process(receipt[key]) for key in ('desktop', 'bridge', 'manager')):
            return False
        if not _descendant(receipt['bridge'], receipt['desktop']):
            return False
        return now is not None or 0 <= time.time() - receipt['heartbeat'] <= LEASE_SECONDS
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error):
        return False


class Handshake:
    """入力成功と完全stdout書込みの後だけ通知される小さな状態機械。"""
    def __init__(self, protocol='initialized_notification'):
        if protocol not in ('initialized_notification', 'post_initialize_response'):
            raise ValueError('Desktopの初期化手順を確認できません')
        self.protocol = protocol
        self.ident = None
        self.delivered_ok = False
        self.complete = False
        self.initialized_forwarded = False
        self.pending_ident = None
        self.lock = threading.RLock()

    def forwarded(self, message):
        with self.lock:
            if message.get('method') == 'initialize':
                ident = message.get('id')
                self.ident = ident if type(ident) in (int, str) else None
                self.delivered_ok = self.complete = False
                self.initialized_forwarded = False
                self.pending_ident = None
            elif message.get('method') == 'initialized' and 'id' not in message:
                self.initialized_forwarded = True
                if self.protocol == 'initialized_notification':
                    self.complete = self.ident is not None and self.delivered_ok
            elif (self.protocol == 'post_initialize_response' and self.delivered_ok
                    and not self.complete and self.pending_ident is None
                    and message.get('method') in ('config/read', 'configRequirements/read',
                                                  'account/read', 'thread/list')
                    and type(message.get('id')) in (int, str)
                    and not (type(message['id']) is type(self.ident)
                             and message['id'] == self.ident)):
                self.pending_ident = message['id']

    def delivered(self, message):
        with self.lock:
            if (self.ident is not None and type(message.get('id')) is type(self.ident)
                    and message.get('id') == self.ident and 'method' not in message):
                result = message.get('result')
                self.delivered_ok = ('error' not in message and isinstance(result, dict)
                                     and isinstance(result.get('userAgent'), str)
                                     and bool(result['userAgent']))
                if not self.delivered_ok:
                    self.complete = False
                    self.pending_ident = None
            elif (self.protocol == 'post_initialize_response' and self.delivered_ok
                    and self.pending_ident is not None
                    and type(message.get('id')) is type(self.pending_ident)
                    and message.get('id') == self.pending_ident and 'method' not in message):
                self.pending_ident = None
                self.complete = 'error' not in message and isinstance(message.get('result'), dict)


class RelayReceipt(Handshake):
    def __init__(self, config):
        protocol = ('post_initialize_response'
                    if sys.platform == 'win32' and config.get('mode') == 'managed'
                    and config.get('initialization_protocol') == 'post_initialize_response'
                    else 'initialized_notification')
        super().__init__(protocol=protocol)
        self.config = config
        self.nonce = config['launch_nonce']
        self.stop = threading.Event()
        self.thread = None
        self.identity = process_identity(os.getpid())
        self.endpoint = endpoint_identity(control_socket())

    def start(self):
        deadline = time.monotonic() + LEASE_SECONDS
        while True:
            try:
                with closing(_session_writer(self.config['session_home'])) as db:
                    db.execute('BEGIN IMMEDIATE')
                    state = load(db)
                    if (state.get('nonce') != self.nonce
                            or state.get('status') not in ('starting', 'connecting', 'bound')
                            or state.get('bridge') is not None or self.stop.is_set()):
                        raise ValueError('別の中継または失効済み接続を検出しました')
                    state.update(bridge=self.identity, status='connecting', heartbeat=0)
                    store(db, state)
                    db.execute('COMMIT')
                break
            except sqlite3.OperationalError as error:
                if not _busy(error):
                    raise
                if time.monotonic() >= deadline or self.stop.wait(BUSY_RETRY_SECONDS):
                    raise ValueError('接続証明の保存を確認できません') from None
        self.thread = threading.Thread(target=self._heartbeat, daemon=True, name='desktop-binding')
        self.thread.start()
        return self

    def _heartbeat_once(self, db):
        state = load(db)
        if (state.get('nonce') != self.nonce or state.get('bridge') != self.identity
                or state.get('status') not in ('starting', 'connecting', 'bound')):
            return False
        observed_at = time.time()
        if state.get('status') == 'bound' and not 0 <= observed_at - state['heartbeat'] <= LEASE_SECONDS:
            return False
        with self.lock:
            handshake = (self.ident, self.delivered_ok, self.complete, self.initialized_forwarded)
        if handshake[2]:
            from connection_settings import validate_executable
            validate_executable(self.config, require_confirmation=False)
            if 'desktop' not in state:
                return True
            if (not _same_process(state['desktop']) or not _same_process(state['manager'])
                    or desktop_bundle(state['desktop']['pid']) != state['bundle_id']
                    or not _descendant(self.identity, state['desktop'])
                    or endpoint_identity(control_socket()) != self.endpoint):
                return False
        # Acquire SQLite's nonblocking writer before taking the receipt lock.
        db.execute('BEGIN IMMEDIATE')
        try:
            current = load(db)
            if current != state:
                db.execute('ROLLBACK')
                return True
            # An input thread may hold this lock during ws.send. Never wait
            # for it while owning the database writer needed by the sender.
            if not self.lock.acquire(blocking=False):
                db.execute('ROLLBACK')
                return True
            try:
                now = time.time()
                if (self.stop.is_set() or not 0 <= now - observed_at <= LEASE_SECONDS
                        or (not handshake[2] and current.get('status') == 'bound'
                            and not 0 <= now - current['heartbeat'] <= LEASE_SECONDS)):
                    db.execute('ROLLBACK')
                    return False
                if handshake != (self.ident, self.delivered_ok, self.complete,
                                 self.initialized_forwarded):
                    db.execute('ROLLBACK')
                    return True
                if handshake[2]:
                    current.update(status='bound', heartbeat=observed_at, endpoint=self.endpoint,
                                   initialize_delivered=True,
                                   initialized_forwarded=self.initialized_forwarded,
                                   initialization_evidence=self.protocol)
                else:
                    current.update(status='connecting', heartbeat=0)
                # The writer is already held and busy_timeout is zero. Keep
                # initialize from invalidating the handshake before commit.
                store(db, current)
                db.execute('COMMIT')
                return True
            finally:
                self.lock.release()
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise

    def _heartbeat(self):
        try:
            while not self.stop.is_set():
                try:
                    with closing(_session_writer(self.config['session_home'])) as db:
                        while not self.stop.is_set():
                            try:
                                if not self._heartbeat_once(db):
                                    return
                            except sqlite3.OperationalError as error:
                                if not _busy(error):
                                    raise
                                self.stop.wait(BUSY_RETRY_SECONDS)
                                continue
                            self.stop.wait(1)
                except sqlite3.OperationalError as error:
                    if not _busy(error):
                        raise
                    self.stop.wait(BUSY_RETRY_SECONDS)
        except Exception:
            pass
        finally:
            self._invalidate()

    def _invalidate(self):
        try:
            with closing(_session_writer(self.config['session_home'])) as db:
                db.execute('BEGIN IMMEDIATE')
                state = load(db)
                if (state.get('nonce') == self.nonce and state.get('bridge') in (None, self.identity)
                        and state.get('status') != 'fallback'):
                    state.update(status='disconnected', heartbeat=0)
                    store(db, state)
                db.execute('COMMIT')
        except sqlite3.OperationalError as error:
            if not _busy(error):
                raise
            # Do not stall relay teardown behind a sender. No lease is renewed;
            # the existing TTL and dead bridge PID continue to reject admission.

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(2)
        self._invalidate()
