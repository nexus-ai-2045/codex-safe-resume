"""Heartbeat の競合を一時DBとmockで再現し、実Desktopには接続しない。"""
from contextlib import ExitStack
import io
from pathlib import Path, PureWindowsPath
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
import desktop_binding as binding
import desktop_stdio


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        self.identity = dict(pid=20, ppid=1, start=[1, 2], executable='/fixture/Desktop')
        self.config = dict(launch_nonce='nonce', session_home=str(self.home), sha256='hash',
                           mode='managed', manual_exclusion_acknowledged=True)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(binding, 'process_identity', return_value=self.identity))
        self.stack.enter_context(patch.object(binding, 'endpoint_identity', return_value={}))
        self.stack.enter_context(patch.object(binding, 'desktop_bundle', return_value='bundle'))
        self.stack.enter_context(patch('connection_settings.validate_executable'))
        self.receipt = binding.RelayReceipt(self.config)
        self.receipt.forwarded({'id': 1, 'method': 'initialize'})
        self.receipt.delivered({'id': 1, 'result': {'userAgent': 'fixture'}})
        self.receipt.forwarded({'method': 'initialized'})
        self.initial = time.time()
        binding.store(self.db, dict(nonce='nonce', status='bound', heartbeat=self.initial,
                                   bridge=self.identity, desktop=self.identity, manager=self.identity,
                                   bundle_id='bundle', endpoint={}, sha256='hash',
                                   initialize_delivered=True, initialized_forwarded=True))
        self.errors = []

    def run_heartbeat(self):
        def run():
            try:
                self.receipt._heartbeat()
            except Exception as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=run, name='fixture-heartbeat')
        self.receipt.thread = thread
        thread.start()
        self.addCleanup(self.stop_heartbeat)
        return thread

    def stop_heartbeat(self):
        if self.db.in_transaction:
            self.db.execute('ROLLBACK')
        self.receipt.stop.set()
        self.receipt.thread.join(2)
        self.assertFalse(self.receipt.thread.is_alive())

    def wait_for(self, predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.01)
        return False

    def quick_connections(self, *, begin_wait=None, begin_release=None):
        """短い実SQLite busyと、BEGIN中の任意のthread切替を再現する。"""
        original = sqlite3.connect
        target = self.home / 'resume.sqlite3'
        class ProbeConnection(sqlite3.Connection):
            def execute(db, sql, *args, **kwargs):
                if (begin_wait is not None and sql == 'BEGIN IMMEDIATE'
                        and threading.current_thread().name == 'fixture-heartbeat'):
                    begin_wait.set()
                    begin_release.wait(1)
                return super().execute(sql, *args, **kwargs)
        def connect(*args, **kwargs):
            kwargs['factory'] = ProbeConnection
            return original(*args, **kwargs)
        def checked_connection(home, *, readonly=False):
            self.assertEqual(Path(home), self.home)
            return connect(target.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                           uri=True, timeout=.05, isolation_level=None)
        self.stack.enter_context(patch.object(binding, 'connection', side_effect=checked_connection))
        self.stack.enter_context(patch.object(binding.sqlite3, 'connect', side_effect=connect))

    def test_database_wait_does_not_hold_relay_lock(self):
        waiting, release, progressed = threading.Event(), threading.Event(), threading.Event()
        self.quick_connections(begin_wait=waiting, begin_release=release)
        self.run_heartbeat()
        self.assertTrue(waiting.wait(1))
        def relay_io():
            self.receipt.forwarded({'id': 2, 'method': 'initialize'})
            message = {'id': 2, 'result': {'userAgent': 'fixture'}}
            desktop_stdio._write_all(io.BytesIO(), b'complete\n', threading.Event(),
                                     receipt=self.receipt, message=message)
            self.receipt.forwarded({'method': 'initialized'})
            progressed.set()
        io_thread = threading.Thread(target=relay_io)
        io_thread.start()
        try:
            self.assertTrue(progressed.wait(.2), 'DB wait blocked Desktop relay I/O')
        finally:
            release.set()
            io_thread.join(2)

    def test_transient_busy_retries_and_preserves_existing_lease(self):
        self.quick_connections()
        self.db.execute('BEGIN IMMEDIATE')
        thread = self.run_heartbeat()
        time.sleep(.2)
        try:
            self.assertTrue(thread.is_alive(), 'busy killed the heartbeat')
            self.assertEqual(binding.load(self.db)['heartbeat'], self.initial)
        finally:
            self.db.execute('ROLLBACK')
        self.assertTrue(self.wait_for(lambda: binding.load(self.db)['heartbeat'] > self.initial))
        self.assertEqual(binding.load(self.db)['status'], 'bound')
        self.assertEqual(self.errors, [])

    def test_busy_past_lease_expiry_never_rebinds(self):
        self.quick_connections()
        self.db.execute('BEGIN IMMEDIATE')
        thread = self.run_heartbeat()
        with patch.object(binding.time, 'time', return_value=self.initial + binding.LEASE_SECONDS + 1):
            time.sleep(.15)
            self.db.execute('ROLLBACK')
            self.assertTrue(self.wait_for(lambda: not thread.is_alive()))
        state = binding.load(self.db)
        # Invalidation itself may encounter the same writer. The persisted
        # receipt must then remain expired, never receive a replacement lease.
        self.assertLessEqual(state['heartbeat'], self.initial)
        self.assertFalse(binding.live_binding(self.config, now=self.initial + binding.LEASE_SECONDS + 1))

    def test_close_during_busy_returns_without_waiting_for_writer(self):
        self.db.execute('BEGIN IMMEDIATE')
        finished = threading.Event()
        def close():
            try:
                self.receipt.close()
            except Exception as exc:
                self.errors.append(exc)
            finally:
                finished.set()
        thread = threading.Thread(target=close)
        thread.start()
        try:
            self.assertTrue(finished.wait(.3), 'close waited for a DB writer')
            self.assertEqual(self.errors, [])
            self.assertEqual(binding.load(self.db)['heartbeat'], self.initial)
        finally:
            self.db.execute('ROLLBACK')
            thread.join(6)

    def test_non_busy_database_failure_stops_and_invalidates(self):
        self.quick_connections()
        original = binding.load
        calls = 0
        def damaged_once(db):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError('no such table: fixture')
            return original(db)
        with patch.object(binding, 'load', side_effect=damaged_once):
            thread = self.run_heartbeat()
            self.assertTrue(self.wait_for(lambda: not thread.is_alive()))
        self.assertEqual(binding.load(self.db)['status'], 'disconnected')

    def test_new_nonce_is_not_changed_by_old_heartbeat_or_close(self):
        binding.store(self.db, dict(nonce='new', status='bound', heartbeat=self.initial,
                                   bridge=self.identity))
        thread = self.run_heartbeat()
        self.assertTrue(self.wait_for(lambda: not thread.is_alive()))
        self.receipt.close()
        self.assertEqual(binding.load(self.db)['nonce'], 'new')
        self.assertEqual(binding.load(self.db)['status'], 'bound')

    def test_handshake_changed_during_database_wait_cannot_refresh_old_proof(self):
        waiting, release = threading.Event(), threading.Event()
        self.quick_connections(begin_wait=waiting, begin_release=release)
        thread = self.run_heartbeat()
        self.assertTrue(waiting.wait(1))
        try:
            self.receipt.forwarded({'id': 2, 'method': 'initialize'})
        finally:
            release.set()
        time.sleep(.1)
        state = binding.load(self.db)
        self.assertLessEqual(state['heartbeat'], self.initial)
        self.assertTrue(thread.is_alive())

    def test_start_retries_busy_without_claiming_or_blocking_handshake(self):
        binding.store(self.db, dict(nonce='nonce', status='starting', heartbeat=0,
                                   desktop=self.identity, manager=self.identity, bundle_id='bundle'))
        self.db.execute('BEGIN IMMEDIATE')
        started = threading.Event()
        def start():
            try:
                self.receipt.start()
                started.set()
            except Exception as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=start)
        thread.start()
        try:
            self.assertFalse(started.wait(.15))
            self.assertNotIn('bridge', binding.load(self.db))
            acquired = self.receipt.lock.acquire(timeout=.1)
            self.assertTrue(acquired)
            if acquired:
                self.receipt.lock.release()
        finally:
            self.db.execute('ROLLBACK')
            thread.join(2)
            if self.receipt.thread is not None:
                self.addCleanup(self.stop_heartbeat)
        self.assertFalse(thread.is_alive())
        self.assertTrue(started.is_set())
        self.assertEqual(self.errors, [])

    def test_session_writer_does_not_create_missing_state(self):
        absent = Path(self.temp.name) / 'missing'
        with self.assertRaises(OSError):
            binding._session_writer(absent)
        self.assertFalse(absent.exists())

    def test_session_writer_keeps_core_version_rejection(self):
        self.db.execute('PRAGMA user_version=999')
        with self.assertRaises(RuntimeError):
            binding._session_writer(self.home)

    def session_writer_with_windows_alias(self, *, reader_alias=False, writer_alias=False,
                                          different_target=False):
        # SQLite may report the 8.3 TEMP spelling used to open the file while
        # Windows Path.resolve() expands it. Only the OS spelling is mocked;
        # validation and both SQLite handles still use the real temporary DB.
        alias = r'C:\fixture\TEMP~1\state\resume.sqlite3'
        actual = (self.home / 'resume.sqlite3').resolve()
        resolved = actual.with_name('other.sqlite3') if different_target else actual
        original_connection, original_connect = binding.connection, sqlite3.connect
        class WindowsAlias(PureWindowsPath):
            def resolve(path):
                return resolved
        def path(value):
            return WindowsAlias(value) if str(value) == alias else Path(value)
        class ConnectionView:
            def __init__(view, db):
                view.db = db
            def execute(view, sql, *args, **kwargs):
                cursor = view.db.execute(sql, *args, **kwargs)
                if sql == 'PRAGMA database_list':
                    row = cursor.fetchone()
                    cursor.close()
                    class Row:
                        def fetchone(row_view):
                            return (*row[:2], alias)
                    return Row()
                return cursor
            def close(view):
                view.db.close()
        def reader(*args, **kwargs):
            db = original_connection(*args, **kwargs)
            return ConnectionView(db) if reader_alias else db
        def connect(target, *args, **kwargs):
            db = original_connect(target, *args, **kwargs)
            return ConnectionView(db) if writer_alias and str(target).endswith('?mode=rw') else db
        with patch.object(binding, 'connection', side_effect=reader), \
                patch.object(binding.sqlite3, 'connect', side_effect=connect), \
                patch.object(binding, 'Path', side_effect=path):
            return binding._session_writer(self.home)

    def test_reader_windows_short_path_matches_the_same_resolved_database(self):
        db = self.session_writer_with_windows_alias(reader_alias=True)
        try:
            self.assertEqual(binding.load(db)['nonce'], 'nonce')
        finally:
            db.close()

    def test_writer_windows_short_path_matches_the_same_resolved_database(self):
        db = self.session_writer_with_windows_alias(writer_alias=True)
        try:
            self.assertEqual(binding.load(db)['nonce'], 'nonce')
        finally:
            db.close()

    def test_windows_alias_resolving_to_another_database_is_rejected(self):
        for key in ('reader_alias', 'writer_alias'):
            with self.subTest(handle=key), self.assertRaises(ValueError):
                self.session_writer_with_windows_alias(**{key: True}, different_target=True)

    def test_fresh_validation_can_replace_lease_that_expires_during_check(self):
        with patch.object(binding.time, 'time', side_effect=[self.initial + 7, self.initial + 9]):
            self.assertTrue(self.receipt._heartbeat_once(self.db))
        self.assertEqual(binding.load(self.db)['heartbeat'], self.initial + 7)
        self.assertTrue(binding.live_binding(self.config, now=self.initial + 9))

    def test_handshake_check_and_commit_are_atomic_after_writer_acquisition(self):
        original = binding.store
        complete_at_store = []
        contenders = []
        forwarded = threading.Event()
        def initialize():
            self.receipt.forwarded({'id': 2, 'method': 'initialize'})
            forwarded.set()
        def store_with_competing_initialize(db, state):
            thread = threading.Thread(target=initialize)
            contenders.append(thread)
            thread.start()
            # Give the Desktop input thread a chance to invalidate the proof
            # after its final check but before the database stores that proof.
            forwarded.wait(.1)
            complete_at_store.append(self.receipt.complete)
            original(db, state)
        try:
            with patch.object(binding, 'store', side_effect=store_with_competing_initialize), \
                    patch.object(binding.time, 'time', return_value=self.initial + 1):
                self.assertTrue(self.receipt._heartbeat_once(self.db))
        finally:
            for thread in contenders:
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.assertEqual(complete_at_store, [True])
        self.assertTrue(forwarded.is_set())
        self.assertFalse(self.receipt.complete)

    def test_contended_final_receipt_lock_releases_database_writer(self):
        acquired, release = threading.Event(), threading.Event()
        def input_send():
            with self.receipt.lock:
                acquired.set()
                release.wait(1)
        contender = threading.Thread(target=input_send)
        underlying = self.db
        class Database:
            @property
            def in_transaction(db):
                return underlying.in_transaction
            def execute(db, sql, *args):
                result = underlying.execute(sql, *args)
                if sql == 'BEGIN IMMEDIATE':
                    contender.start()
                    if not acquired.wait(1):
                        raise AssertionError('input thread did not acquire the receipt lock')
                return result
        before = binding.load(self.db)
        began = time.monotonic()
        try:
            self.assertTrue(self.receipt._heartbeat_once(Database()))
            self.assertLess(time.monotonic() - began, .2)
            self.assertFalse(self.db.in_transaction)
            self.assertEqual(binding.load(self.db), before)
        finally:
            release.set()
            contender.join(2)
