from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import core
import desktop_binding as binding
import desktop_stdio


PROTOCOL = 'post_initialize_response'


class WindowsHandshakeTests(unittest.TestCase):
    def ready(self, protocol=PROTOCOL):
        handshake = binding.Handshake(protocol=protocol)
        handshake.forwarded({'id': '__codex_initialize__', 'method': 'initialize'})
        handshake.delivered({'id': '__codex_initialize__', 'result': {'userAgent': 'Desktop'}})
        return handshake

    def test_initialize_reply_alone_is_insufficient(self):
        handshake = self.ready()
        self.assertTrue(handshake.delivered_ok)
        self.assertFalse(handshake.complete)

    def test_allowlisted_read_and_delivered_response_complete(self):
        for method in ('config/read', 'configRequirements/read', 'account/read', 'thread/list'):
            with self.subTest(method=method):
                handshake = self.ready()
                handshake.forwarded({'id': 5, 'method': method})
                self.assertFalse(handshake.complete)
                handshake.delivered({'id': 5, 'result': {}})
                self.assertTrue(handshake.complete)
                self.assertFalse(handshake.initialized_forwarded)

    def test_unknown_and_non_read_methods_cannot_complete(self):
        for method in ('thread/start', 'turn/start', 'unknown', 'initialized'):
            with self.subTest(method=method):
                handshake = self.ready()
                handshake.forwarded({'id': 5, 'method': method})
                handshake.delivered({'id': 5, 'result': {}})
                self.assertFalse(handshake.complete)

    def test_pending_request_is_not_overwritten(self):
        handshake = self.ready()
        handshake.forwarded({'id': 5, 'method': 'config/read'})
        handshake.forwarded({'id': 6, 'method': 'account/read'})
        handshake.delivered({'id': 6, 'result': {}})
        self.assertFalse(handshake.complete)
        handshake.delivered({'id': 5, 'result': {}})
        self.assertTrue(handshake.complete)

    def test_error_or_invalid_result_cannot_complete(self):
        for response in ({'error': {}}, {'result': None}, {'result': []},
                         {'result': {}, 'error': None}, {'method': 'config/read', 'result': {}}):
            with self.subTest(response=response):
                handshake = self.ready()
                handshake.forwarded({'id': 5, 'method': 'config/read'})
                handshake.delivered({'id': 5, **response})
                self.assertFalse(handshake.complete)

    def test_matching_error_allows_a_later_request(self):
        handshake = self.ready()
        handshake.forwarded({'id': 5, 'method': 'config/read'})
        handshake.delivered({'id': 5, 'error': {}})
        handshake.forwarded({'id': 6, 'method': 'account/read'})
        handshake.delivered({'id': 6, 'result': {}})
        self.assertTrue(handshake.complete)

    def test_typed_ids_do_not_match_by_coercion(self):
        for ident in (True, 1.0, '1', None):
            with self.subTest(ident=ident):
                handshake = self.ready()
                handshake.forwarded({'id': 1, 'method': 'config/read'})
                handshake.delivered({'id': ident, 'result': {}})
                self.assertFalse(handshake.complete)

    def test_string_request_id_matches_only_string_response(self):
        handshake = self.ready()
        handshake.forwarded({'id': '1', 'method': 'config/read'})
        handshake.delivered({'id': 1, 'result': {}})
        self.assertFalse(handshake.complete)
        handshake.delivered({'id': '1', 'result': {}})
        self.assertTrue(handshake.complete)

    def test_failed_initialize_cannot_enable_read_proof(self):
        for response in ({'error': {}}, {'result': {}}, {'result': {'userAgent': ''}}):
            with self.subTest(response=response):
                handshake = binding.Handshake(protocol=PROTOCOL)
                handshake.forwarded({'id': 1, 'method': 'initialize'})
                handshake.delivered({'id': 1, **response})
                handshake.forwarded({'id': 5, 'method': 'config/read'})
                handshake.delivered({'id': 5, 'result': {}})
                self.assertFalse(handshake.complete)

    def test_partial_delivery_cannot_complete(self):
        handshake = self.ready()
        handshake.forwarded({'id': 5, 'method': 'config/read'})
        class FailingOutput(io.BytesIO):
            def write(self, value):
                if self.tell():
                    raise OSError('fixture')
                return super().write(value[:2])
        with self.assertRaises(OSError):
            desktop_stdio._write_all(FailingOutput(), b'{"id":5,"result":{}}\n',
                                     threading.Event(), receipt=handshake,
                                     message={'id': 5, 'result': {}})
        self.assertFalse(handshake.complete)

    def test_invalid_request_ids_do_not_create_pending_proof(self):
        for ident in (True, 5.0, None, [], '__codex_initialize__'):
            with self.subTest(ident=ident):
                handshake = self.ready()
                handshake.forwarded({'id': ident, 'method': 'config/read'})
                handshake.delivered({'id': ident, 'result': {}})
                self.assertFalse(handshake.complete)

    def test_early_read_is_not_retroactively_accepted(self):
        handshake = binding.Handshake(protocol=PROTOCOL)
        handshake.forwarded({'id': 1, 'method': 'initialize'})
        handshake.forwarded({'id': 5, 'method': 'config/read'})
        handshake.delivered({'id': 1, 'result': {'userAgent': 'Desktop'}})
        handshake.delivered({'id': 5, 'result': {}})
        self.assertFalse(handshake.complete)

    def test_new_initialize_clears_previous_pending_and_completion(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                handshake = self.ready()
                handshake.forwarded({'id': 5, 'method': 'config/read'})
                if completed:
                    handshake.delivered({'id': 5, 'result': {}})
                handshake.forwarded({'id': 2, 'method': 'initialize'})
                self.assertFalse(handshake.complete)
                self.assertFalse(handshake.delivered_ok)
                handshake.delivered({'id': 2, 'result': {'userAgent': 'Desktop'}})
                handshake.delivered({'id': 5, 'result': {}})
                self.assertFalse(handshake.complete)

    def test_initialized_observation_is_truthful_and_separate(self):
        handshake = self.ready()
        handshake.forwarded({'method': 'initialized'})
        self.assertTrue(handshake.initialized_forwarded)
        self.assertFalse(handshake.complete)
        handshake.forwarded({'id': 2, 'method': 'initialize'})
        self.assertFalse(handshake.initialized_forwarded)

    def test_default_protocol_keeps_notification_requirement(self):
        handshake = binding.Handshake()
        handshake.forwarded({'id': 1, 'method': 'initialize'})
        handshake.delivered({'id': 1, 'result': {'userAgent': 'Desktop'}})
        handshake.forwarded({'id': 5, 'method': 'config/read'})
        handshake.delivered({'id': 5, 'result': {}})
        self.assertFalse(handshake.complete)
        handshake.forwarded({'method': 'initialized'})
        self.assertTrue(handshake.complete)

    def test_unknown_protocol_is_rejected(self):
        with self.assertRaises(ValueError):
            binding.Handshake(protocol='unknown')


class WindowsReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.home = Path(self.temp) / 'state'
        self.db = core.connection(self.home)
        self.addCleanup(self.db.close)
        self.identity = dict(pid=20, ppid=1, start=[1, 2], executable='/fixture/Desktop')
        self.config = dict(launch_nonce='nonce', session_home=str(self.home), sha256='hash',
                           mode='managed', manual_exclusion_acknowledged=True,
                           initialization_protocol=PROTOCOL)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(binding, 'process_identity', return_value=self.identity))
        self.stack.enter_context(patch.object(binding, 'endpoint_identity', return_value={}))
        self.stack.enter_context(patch.object(binding, 'desktop_bundle', return_value='bundle'))
        self.stack.enter_context(patch.object(binding, '_descendant', return_value=True))
        self.stack.enter_context(patch('connection_settings.validate_executable'))
        self.stack.enter_context(patch.object(binding.sys, 'platform', 'win32'))
        binding.store(self.db, dict(nonce='nonce', status='connecting', heartbeat=0,
                                   bridge=self.identity, desktop=self.identity, manager=self.identity,
                                   bundle_id='bundle', endpoint={}, sha256='hash'))

    def complete(self, config=None):
        receipt = binding.RelayReceipt(self.config if config is None else config)
        receipt.forwarded({'id': 1, 'method': 'initialize'})
        receipt.delivered({'id': 1, 'result': {'userAgent': 'Desktop'}})
        receipt.forwarded({'id': 5, 'method': 'config/read'})
        receipt.delivered({'id': 5, 'result': {}})
        return receipt

    def test_heartbeat_records_evidence_without_fabricating_notification(self):
        receipt = self.complete()
        self.assertTrue(receipt._heartbeat_once(self.db))
        state = binding.load(self.db)
        self.assertEqual(state['status'], 'bound')
        self.assertIs(state['initialize_delivered'], True)
        self.assertIs(state['initialized_forwarded'], False)
        self.assertEqual(state['initialization_evidence'], PROTOCOL)
        self.assertTrue(binding.live_binding(self.config))

    def test_receipt_selection_requires_explicit_managed_windows_protocol(self):
        for platform, updates in (('darwin', {}), ('win32', {'mode': 'prepared'}),
                                  ('win32', {'mode': 'manual'}),
                                  ('win32', {'initialization_protocol': None}),
                                  ('win32', {'initialization_protocol': 'unknown'})):
            with self.subTest(platform=platform, updates=updates), \
                    patch.object(binding.sys, 'platform', platform):
                self.assertFalse(self.complete({**self.config, **updates}).complete)

    def test_heartbeat_preserves_actual_notification_observation(self):
        receipt = self.complete()
        receipt.forwarded({'method': 'initialized'})
        self.assertTrue(receipt._heartbeat_once(self.db))
        self.assertIs(binding.load(self.db)['initialized_forwarded'], True)

    def test_live_binding_requires_matching_windows_protocol(self):
        self.complete()._heartbeat_once(self.db)
        for platform, updates in (('darwin', {}), ('win32', {'mode': 'prepared'}),
                                  ('win32', {'initialization_protocol': None}),
                                  ('win32', {'initialization_protocol': 'unknown'})):
            with self.subTest(platform=platform, updates=updates), \
                    patch.object(binding.sys, 'platform', platform):
                self.assertFalse(binding.live_binding({**self.config, **updates}))

    def test_live_binding_rejects_missing_evidence_or_failed_initialization(self):
        self.complete()._heartbeat_once(self.db)
        valid = binding.load(self.db)
        for changes in ({'initialization_evidence': None}, {'initialization_evidence': 'unknown'},
                        {'initialize_delivered': False}, {'heartbeat': time.time() - 20}):
            with self.subTest(changes=changes):
                binding.store(self.db, {**valid, **changes})
                self.assertFalse(binding.live_binding(self.config))

    def test_post_protocol_retains_native_checks(self):
        self.complete()._heartbeat_once(self.db)
        for target in ('_same_process', '_descendant'):
            with self.subTest(target=target), patch.object(binding, target, return_value=False):
                self.assertFalse(binding.live_binding(self.config))
        with patch.object(binding, 'endpoint_identity', return_value={'changed': True}):
            self.assertFalse(binding.live_binding(self.config))

    def bound_at(self, timestamp):
        receipt = self.complete()
        state = binding.load(self.db)
        state.update(status='bound', heartbeat=timestamp)
        binding.store(self.db, state)
        return receipt

    def test_fresh_validation_can_replace_lease_expiring_during_validation(self):
        receipt = self.bound_at(96)
        with patch.object(binding.time, 'time', side_effect=[100, 106]):
            self.assertTrue(receipt._heartbeat_once(self.db))
        state = binding.load(self.db)
        self.assertEqual(state['heartbeat'], 100)
        self.assertEqual(state['status'], 'bound')

    def test_expired_lease_at_validation_start_is_rejected(self):
        receipt = self.bound_at(91)
        before = binding.load(self.db)
        with patch.object(binding.time, 'time', return_value=100):
            self.assertFalse(receipt._heartbeat_once(self.db))
        self.assertEqual(binding.load(self.db), before)

    def test_validation_exceeding_lease_budget_is_rejected(self):
        receipt = self.bound_at(96)
        before = binding.load(self.db)
        with patch.object(binding.time, 'time', side_effect=[100, 109]):
            self.assertFalse(receipt._heartbeat_once(self.db))
        self.assertEqual(binding.load(self.db), before)

    def test_stop_during_valid_validation_cannot_renew(self):
        receipt = self.bound_at(96)
        before = binding.load(self.db)
        def stop(_child, _parent):
            receipt.stop.set()
            return True
        with patch.object(binding.time, 'time', side_effect=[100, 106]), \
                patch.object(binding, '_descendant', side_effect=stop):
            self.assertFalse(receipt._heartbeat_once(self.db))
        self.assertEqual(binding.load(self.db), before)

    def test_changed_nonce_or_state_during_validation_is_preserved(self):
        for changes in ({'nonce': 'other'}, {'status': 'disconnected'}):
            with self.subTest(changes=changes):
                receipt = self.bound_at(96)
                def change(_child, _parent):
                    binding.store(self.db, {**binding.load(self.db), **changes})
                    return True
                with patch.object(binding.time, 'time', return_value=100), \
                        patch.object(binding, '_descendant', side_effect=change):
                    self.assertTrue(receipt._heartbeat_once(self.db))
                state = binding.load(self.db)
                self.assertEqual(state['heartbeat'], 96)
                for key, value in changes.items():
                    self.assertEqual(state[key], value)
                state.update(nonce='nonce', status='connecting')
                binding.store(self.db, state)

    def test_live_binding_rejects_lease_expired_by_its_native_checks(self):
        self.complete()._heartbeat_once(self.db)
        state = binding.load(self.db)
        state['heartbeat'] = 96
        binding.store(self.db, state)
        with patch.object(binding.time, 'time', side_effect=[100, 105]):
            self.assertFalse(binding.live_binding(self.config))

    def test_live_binding_explicit_time_remains_a_fixed_sample(self):
        self.complete()._heartbeat_once(self.db)
        state = binding.load(self.db)
        state['heartbeat'] = 96
        binding.store(self.db, state)
        with patch.object(binding.time, 'time', side_effect=AssertionError('unexpected clock')):
            self.assertTrue(binding.live_binding(self.config, now=100))
