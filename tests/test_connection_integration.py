"""子プロセス・WebSocket・SQLiteを通す結合検査。実Desktopの代用とは扱わない。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import core
from desktop_bridge import DesktopBridge, BridgeError
from connection_settings import configure
import watch

T = '123e4567-e89b-12d3-a456-426614174000'


class NativeConnectionIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.writes = self.base/'writes.jsonl'
        self.mode = 'normal'
        self.spawned = []
        self.original = subprocess.Popen
        self.cli = self.base/'codex-fixture'
        self.cli.write_bytes(b'fixture; never executed directly')
        self.cli.chmod(0o700)
        self.config = dict(mode='guarded', executable=str(self.cli),
                           sha256=hashlib.sha256(self.cli.read_bytes()).hexdigest(),
                           desktop_confirmed=True, manual_exclusion_acknowledged=True)
        fixture = Path(__file__).parent/'fixtures'/'proxy_cli.py'
        def spawn(args, *positional, **kwargs):
            # Only official-command shapes are rewritten to the isolated fixture.
            if str(args[0]) == '/bin/ls' or str(args[0]).lower().endswith('windowspowershell/v1.0/powershell.exe') or str(args[0]).lower().endswith(r'windowspowershell\v1.0\powershell.exe'):
                return self.original(args, *positional, **kwargs)
            self.assertEqual(args[0], str(self.cli))
            self.assertIn(args[1:], [['app-server', 'proxy'], ['app-server', 'daemon', 'version']])
            kwargs['env'] = dict(kwargs.get('env') or os.environ,
                                 FIXTURE_MODE=self.mode, FIXTURE_WRITES=str(self.writes))
            process = self.original([sys.executable, str(fixture), *args[1:]], *positional, **kwargs)
            self.spawned.append(process)
            return process
        self.patch = patch('subprocess.Popen', side_effect=spawn)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tearDown(self):
        for child in self.spawned:
            if child.poll() is None:
                child.kill(); child.wait()
                self.fail('接続が子プロセスを回収していません')

    def bridge(self):
        return DesktopBridge(home=self.base/'codex', config=self.config, timeout=2)

    def count(self):
        return len(self.writes.read_text().splitlines()) if self.writes.exists() else 0

    def test_real_handshake_and_one_continue_request(self):
        with self.bridge() as bridge:
            self.assertTrue(bridge.capabilities()['guarded_admission'])
            self.assertFalse(bridge.capabilities()['safe_admission'])
            bridge.prepare_thread(T)
            result = bridge.send_once(T, 'failure', attempt_id='123e4567-e89b-12d3-a456-426614174001')
            self.assertEqual(result['turn']['id'], 'resumed')
        self.assertEqual(self.count(), 1)
        self.assertFalse(bridge.capabilities()['guarded_admission'])

    def test_lost_reply_and_restart_never_resend_saved_intent(self):
        self.mode = 'lost-reply'
        db = core.connection(self.base/'state')
        try:
            item = dict(id=T, title='デモ', cwd='', limited=True, failure_turn='failure',
                        latest_turn='failure', turn_status='failed', awaiting=False,
                        observation_verified=True)
            core.sync(db, [item]); core.enable(db, T, True); core.quota(db, T, 'failure', int(time.time()) - 1)
            with patch('watch.discover', return_value=([item], None)):
                watch.once(db, self.bridge)
            self.assertEqual(self.count(), 1)
            self.assertFalse(core.list_threads(db)[0]['enabled'])
        finally:
            db.close()
        db = core.connection(self.base/'state')
        try:
            with patch('watch.discover', return_value=([item], None)):
                watch.once(db, self.bridge)
            self.assertEqual(self.count(), 1)
        finally:
            db.close()

    def test_default_monitor_loads_config_and_persists_wire_attempt(self):
        db = core.connection(self.base/'state')
        try:
            configure(db, self.cli, desktop_confirmed=True, manual_exclusion_acknowledged=True)
            item = dict(id=T, title='デモ', cwd='', limited=True, failure_turn='failure',
                        latest_turn='failure', turn_status='failed', awaiting=False,
                        observation_verified=True)
            core.sync(db, [item]); core.enable(db, T, True)
            core.quota(db, T, 'failure', int(time.time()) - 1)
            with patch.dict(os.environ, CODEX_HOME=str(self.base/'codex')), \
                    patch('watch.discover', return_value=([item], None)):
                result = watch.once(db)
            self.assertEqual(result['results'], [(T, 'dispatched')])
            self.assertEqual(core.list_threads(db)[0]['resume_turn'], 'resumed')
            wire = json.loads(self.writes.read_text().strip())
            attempt = db.execute('SELECT attempt_id FROM attempts').fetchone()[0]
            self.assertEqual(wire['clientUserMessageId'], attempt)
            self.assertEqual(self.count(), 1)
        finally:
            db.close()

    def test_approval_request_is_never_answered_or_sent(self):
        self.mode = 'approval'
        with self.bridge() as bridge:
            with self.assertRaises(BridgeError): bridge.snapshot(T)
        self.assertEqual(self.count(), 0)

    def test_version_mismatch_does_not_open_proxy(self):
        self.mode = 'version-mismatch'
        with self.assertRaises(BridgeError):
            with self.bridge(): pass
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.count(), 0)

    def test_no_config_does_not_launch_anything(self):
        with self.assertRaises(BridgeError):
            with DesktopBridge(): pass
        self.assertEqual(self.spawned, [])

    def test_live_turn_receipt_survives_persisted_history_lag(self):
        self.mode = 'history-lag'
        with self.bridge() as bridge:
            result = bridge.send_once(T, 'failure', attempt_id='123e4567-e89b-12d3-a456-426614174001')
            self.assertEqual(result['turn']['id'], 'resumed')
            self.assertEqual(bridge.snapshot(T)['latest_turn'], 'resumed')
        self.assertEqual(self.count(), 1)
