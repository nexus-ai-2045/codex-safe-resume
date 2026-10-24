"""Desktop接続用entryは接続準備だけを使い、server起動や設定無視をしない。"""
import unittest
from copy import deepcopy
from contextlib import ExitStack
from unittest.mock import Mock, patch
from pathlib import Path

import desktop_launcher


class ManagedStartupFallback(unittest.TestCase):
    def setUp(self):
        self.config = dict(mode='managed', launch_nonce='owned-nonce')
        self.cli = Path('/fixture/codex')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('desktop_launcher.connection', return_value=Mock()))
        self.stack.enter_context(patch('desktop_launcher.load', return_value=self.config))
        self.validate = self.stack.enter_context(patch(
            'desktop_launcher.validate_executable', return_value=self.cli))
        self.stack.enter_context(patch.dict('os.environ', CSR_DESKTOP_NONCE='owned-nonce'))
        self.caller = self.stack.enter_context(patch(
            'desktop_binding.validate_startup_caller', create=True))
        self.record = self.stack.enter_context(patch(
            'desktop_binding.record_startup_fallback', create=True))
        self.bridge = self.stack.enter_context(patch('desktop_launcher.DesktopBridge'))
        self.receipt = self.stack.enter_context(patch('desktop_binding.RelayReceipt'))
        self.relay = self.stack.enter_context(patch('desktop_launcher.relay'))
        self.run = self.stack.enter_context(patch('desktop_launcher.subprocess.run'))
        self.run.return_value.returncode = 0

    def assert_fallback(self, args, reason, returncode=0):
        self.assertEqual(desktop_launcher.main(args), returncode)
        self.assertEqual(self.caller.call_count, 2)
        self.assertTrue(all(call.args == (self.config,) for call in self.caller.call_args_list))
        self.record.assert_called_once_with(self.config, reason)
        self.run.assert_called_once()
        self.assertEqual(self.run.call_args.args, ([str(self.cli), *args],))
        self.assertEqual(set(self.run.call_args.kwargs),
                         {'check', 'creationflags', 'stdin', 'stdout', 'stderr'}
                         if desktop_launcher.os.name == 'nt'
                         else {'check', 'stdin', 'stdout', 'stderr'})
        self.assertIs(self.run.call_args.kwargs['stdin'], desktop_launcher.sys.stdin)
        self.assertIs(self.run.call_args.kwargs['stdout'], desktop_launcher.sys.stdout)
        self.assertIs(self.run.call_args.kwargs['stderr'], desktop_launcher.sys.stderr)
        self.assertFalse(self.run.call_args.kwargs['check'])
        self.assertGreaterEqual(self.validate.call_count, 2)
        self.relay.assert_not_called()

    def test_unknown_plugin_option_preserves_exact_official_cli_arguments(self):
        args = ['-c', 'features.code_mode_host=true', 'app-server',
                '--analytics-default-enabled', '--plugin', 'private-plugin-path']
        self.assert_fallback(args, 'unsupported_launch_options')
        self.bridge.assert_not_called()
        self.receipt.assert_not_called()

    def test_shared_transport_failure_preserves_official_cli_returncode(self):
        self.bridge.return_value.__enter__.side_effect = OSError('private-path')
        self.run.return_value.returncode = 17
        self.assert_fallback(['app-server'], 'shared_transport_unavailable', returncode=17)

    def test_shared_settings_mismatch_falls_back_without_losing_configuration(self):
        self.bridge.return_value.__enter__.return_value.request.return_value = {
            'config': {'features': {'prefer_mxc': False}}}
        self.assert_fallback(['app-server', '-c', 'features.prefer_mxc=true'],
                             'shared_settings_mismatch')

    def test_receipt_failure_falls_back_before_relay(self):
        self.receipt.return_value.start.side_effect = OSError('private-path')
        self.assert_fallback(['app-server'], 'receipt_unavailable')

    def test_recording_failure_does_not_break_official_cli_startup(self):
        self.record.side_effect = OSError('private-path')
        self.assert_fallback(['app-server', '--unknown'], 'unsupported_launch_options')

    def test_wrong_nonce_never_falls_back(self):
        self.config['launch_nonce'] = 'different'
        with self.assertRaises(ValueError):
            desktop_launcher.main(['app-server', '--unknown'])
        self.caller.assert_not_called()
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_unverified_caller_never_falls_back(self):
        self.caller.side_effect = ValueError('unverified')
        with self.assertRaises(ValueError):
            desktop_launcher.main(['app-server', '--unknown'])
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_caller_or_request_change_during_probe_prevents_fallback(self):
        self.caller.side_effect = [None, ValueError('request-changed')]
        self.bridge.return_value.__enter__.side_effect = OSError('private-path')
        with self.assertRaises(ValueError):
            desktop_launcher.main(['app-server'])
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_non_server_commands_and_non_stdio_listeners_never_fall_back(self):
        for args in (['exec', 'app-server'], ['auth', 'app-server'],
                     ['daemon', 'app-server'], ['-c', 'app-server'],
                     ['app-server', 'app-server'], ['app-server', '--listen', 'ws://localhost:1']):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    desktop_launcher.main(args)
                self.run.assert_not_called()
                self.record.assert_not_called()

    def test_cli_is_revalidated_immediately_before_fallback(self):
        self.validate.side_effect = [self.cli, ValueError('changed')]
        with self.assertRaises(ValueError):
            desktop_launcher.main(['app-server', '--unknown'])
        self.run.assert_not_called()

    def test_relay_failure_never_retries_or_falls_back(self):
        self.relay.side_effect = OSError('after-stdio-start')
        with self.assertRaises(OSError):
            desktop_launcher.main(['app-server'])
        self.relay.assert_called_once()
        self.receipt.return_value.start.return_value.close.assert_called_once()
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_receipt_close_failure_after_relay_never_falls_back(self):
        self.receipt.return_value.start.return_value.close.side_effect = OSError('close')
        with self.assertRaises(OSError):
            desktop_launcher.main(['app-server'])
        self.relay.assert_called_once()
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_frozen_fallback_inherits_native_stdio_and_hides_console(self):
        with patch.object(desktop_launcher.sys, 'frozen', True, create=True), \
                patch.object(Path, 'samefile', return_value=False):
            self.assert_fallback(['app-server', '--unknown'], 'unsupported_launch_options')
        if desktop_launcher.os.name == 'nt':
            self.assertEqual(self.run.call_args.kwargs['creationflags'],
                             desktop_launcher.subprocess.CREATE_NO_WINDOW)

    def test_revalidation_cannot_redirect_fallback_to_frozen_bridge(self):
        with patch.object(desktop_launcher.sys, 'frozen', True, create=True), \
                patch.object(Path, 'samefile', side_effect=[False, True]):
            with self.assertRaises(ValueError):
                desktop_launcher.main(['app-server', '--unknown'])
        self.run.assert_not_called()

    def test_successful_managed_relay_does_not_launch_another_cli(self):
        self.assertEqual(desktop_launcher.main(['app-server']), 0)
        self.relay.assert_called_once()
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_false_missing_and_empty_nonce_never_fall_back(self):
        for nonce in ('', None):
            with self.subTest(nonce=nonce):
                self.config['launch_nonce'] = nonce
                with self.assertRaises(ValueError):
                    desktop_launcher.main(['app-server', '--unknown'])
                self.run.assert_not_called()

    def test_prepared_mode_never_falls_back_on_transport_or_receipt_failure(self):
        self.config['mode'] = 'prepared'
        self.bridge.return_value.__enter__.side_effect = OSError('private-path')
        with self.assertRaises(OSError):
            desktop_launcher.main(['app-server'])
        self.run.assert_not_called()
        self.caller.assert_not_called()

    def test_unrecognized_global_flag_before_command_never_falls_back(self):
        with self.assertRaises(ValueError):
            desktop_launcher.main(['--unknown', 'private-value', 'app-server'])
        self.run.assert_not_called()
        self.record.assert_not_called()

    def test_successful_known_plugin_launch_checks_nested_enabled_flags(self):
        args = ['-c', 'features.code_mode_host=true', 'app-server',
                '--analytics-default-enabled', '-c', 'features.prefer_mxc=true',
                '-c', 'plugins.codex-app-tools' '@openai-bundled.mcp_servers.codex_app.enabled=true',
                '-c', 'plugins.code-review' '@openai-bundled.mcp_servers.code-review.enabled=true']
        self.bridge.return_value.__enter__.return_value.request.return_value = {
            'config': {
                'features': {'code_mode_host': True, 'prefer_mxc': True},
                'plugins': {
                    'codex-app-tools@openai-bundled': {'mcp_servers': {'codex_app': {'enabled': True}}},
                    'code-review@openai-bundled': {'mcp_servers': {'code-review': {'enabled': True}}},
                },
            },
        }
        self.assertEqual(desktop_launcher.main(args), 0)
        self.relay.assert_called_once()
        self.run.assert_not_called()

    def test_unknown_plugin_configuration_is_forwarded_only_to_verified_official_cli(self):
        self.assert_fallback([
            '-c', 'plugins.unknown' '@private.mcp_servers.private.enabled=true', 'app-server'],
            'unsupported_launch_options')


class SharedPluginSettings(unittest.TestCase):
    def test_known_plugin_options_require_exact_nested_booleans(self):
        settings = desktop_launcher._PLUGIN_SETTINGS
        args = ['app-server']
        for name in sorted(settings):
            args.extend(['-c', name + '=true'])
        self.assertEqual(desktop_launcher._options(args), settings)
        expected = {
            'plugins': {
                'codex-app-tools@openai-bundled': {
                    'enabled': False, 'mcp_servers': {'codex_app': {'enabled': True}}},
                'code-review@openai-bundled': {
                    'enabled': False, 'mcp_servers': {'code-review': {'enabled': True}}},
            },
        }
        probe = Mock()
        probe.request.return_value = {'config': expected}
        desktop_launcher.check_server_settings(probe, settings)
        for name in settings:
            for value in (False, None, 0, 1, 'true'):
                with self.subTest(name=name, value=value):
                    changed = deepcopy(expected)
                    item = changed
                    for key in name.split('.')[:-1]:
                        item = item[key]
                    item['enabled'] = value
                    probe.request.return_value = {'config': changed}
                    with self.assertRaises(ValueError):
                        desktop_launcher.check_server_settings(probe, settings)
            changed = deepcopy(expected)
            item = changed
            for key in name.split('.')[:-1]:
                item = item[key]
            del item['enabled']
            probe.request.return_value = {'config': changed}
            with self.assertRaises(ValueError):
                desktop_launcher.check_server_settings(probe, settings)

    def test_plugin_setting_false_and_unknown_plugin_are_not_relay_options(self):
        for name in (*desktop_launcher._PLUGIN_SETTINGS,
                     'plugins.unknown' '@private.mcp_servers.private.enabled'):
            for value in ('false', '1', '"true"'):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        desktop_launcher._options(['app-server', '-c', name + '=' + value])


class DesktopOptions(unittest.TestCase):
    def test_accepts_only_stdio_transport_and_telemetry_preference(self):
        self.assertFalse(desktop_launcher._options(['app-server', '--analytics-default-enabled']))
        self.assertFalse(desktop_launcher._options(['app-server', '--listen', 'stdio://']))
        self.assertTrue(desktop_launcher._options(['app-server', '-c', 'features.windows_sandbox_service=true']))

    def test_known_global_features_before_and_after_command(self):
        for feature in ('windows_sandbox_service', 'code_mode_host', 'prefer_mxc'):
            for flag in ('-c', '--config'):
                for args in ([flag, f'features.{feature}=true', 'app-server',
                              '--analytics-default-enabled'],
                             ['app-server', flag, f'features.{feature}=true']):
                    with self.subTest(args=args):
                        self.assertEqual(desktop_launcher._options(args), frozenset({feature}))
        self.assertEqual(desktop_launcher._options([
            '-c', 'features.code_mode_host=true', 'app-server',
            '--config', 'features.windows_sandbox_service=true', '--stdio']),
            frozenset({'code_mode_host', 'windows_sandbox_service'}))

    def test_rejects_unknown_or_security_overrides_without_echo(self):
        for args in ([], ['daemon', 'start'], ['app-server', '--listen', 'ws://127.0.0.1:1'],
                     ['app-server', '-c', 'approval_policy="never"'], ['app-server', '--config'],
                     ['app-server', '--unknown-private-value'],
                     ['-c', 'features.code_mode_host=true'], ['-c', 'app-server'],
                     ['app-server', 'app-server'], ['--stdio', 'app-server'],
                     ['--config', 'features.code_mode_host=false', 'app-server'],
                     ['-c', 'features.code_mode_host=1', 'app-server'],
                     ['-c', 'features.unknown=true', 'app-server'],
                     ['app-server', '-c', 'features.prefer_mxc=false'],
                     ['-c', 'features.code_mode_host=true', 'app-server', '--config'],
                     ['-c', 'approval_policy="never"', 'app-server']):
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as raised:
                    desktop_launcher._options(args)
                self.assertNotIn('private-value', str(raised.exception))

    def test_probe_mode_never_enables_resume(self):
        from desktop_bridge import DesktopBridge
        bridge = DesktopBridge(config=dict(mode='prepared'), transport_only=True)
        bridge.connected = True
        self.assertFalse(bridge.capabilities()['guarded_admission'])
        self.assertFalse(bridge.capabilities()['desktop_binding'])

    def test_prepared_connection_probes_then_relays_without_starting_server(self):
        config = dict(mode='prepared')
        cli = Path('/fixture/codex')
        with patch('desktop_launcher.connection', return_value=Mock()), \
                patch('desktop_launcher.load', return_value=config), \
                patch('desktop_launcher.validate_executable', return_value=cli) as validate, \
                patch('desktop_launcher.DesktopBridge') as bridge, \
                patch('desktop_launcher.relay') as relay:
            self.assertEqual(desktop_launcher.main(['app-server', '--analytics-default-enabled']), 0)
            self.assertEqual(validate.call_count, 2)
            self.assertTrue(all(call.kwargs == {'require_confirmation': False}
                                for call in validate.call_args_list))
            bridge.assert_called_once_with(config=config, transport_only=True)
            relay.assert_called_once()
            self.assertEqual(relay.call_args.args, (cli,))

    def test_windows_sandbox_override_must_already_match_shared_server(self):
        with patch('desktop_launcher.connection', return_value=Mock()), \
                patch('desktop_launcher.load', return_value=dict(mode='prepared')), \
                patch('desktop_launcher.validate_executable', return_value=Path('/fixture/codex')), \
                patch('desktop_launcher.DesktopBridge') as bridge, \
                patch('desktop_launcher.relay') as relay:
            probe = bridge.return_value.__enter__.return_value
            probe.request.return_value = {'config': {'features': {'windows_sandbox_service': False}}}
            with self.assertRaises(ValueError):
                desktop_launcher.main(['app-server', '-c', 'features.windows_sandbox_service=true'])
            relay.assert_not_called()
            probe.request.return_value['config']['features']['windows_sandbox_service'] = True
            self.assertEqual(desktop_launcher.main(['app-server', '-c', 'features.windows_sandbox_service=true']), 0)
            relay.assert_called_once()

    def test_every_requested_feature_must_already_be_true_on_shared_server(self):
        cases = [
            (['-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled',
              '-c', 'features.prefer_mxc=true'], {'code_mode_host': True, 'prefer_mxc': True}),
            (['-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled'],
             {'code_mode_host': True}),
            (['app-server', '-c', 'features.code_mode_host=true'], {'code_mode_host': True}),
            (['-c', 'features.code_mode_host=true', 'app-server',
              '-c', 'features.windows_sandbox_service=true'],
             {'code_mode_host': True, 'windows_sandbox_service': True}),
        ]
        for args, requested in cases:
            replies = [({'config': {'features': requested}}, True)]
            for name in requested:
                for value in (False, None, 0, 1, 'true'):
                    replies.append(({'config': {'features': dict(requested, **{name: value})}}, False))
                missing = dict(requested)
                del missing[name]
                replies.append(({'config': {'features': missing}}, False))
            for reply, permitted in replies:
                with self.subTest(args=args, reply=reply):
                    with patch('desktop_launcher.connection', return_value=Mock()), \
                            patch('desktop_launcher.load', return_value=dict(mode='prepared')), \
                            patch('desktop_launcher.validate_executable', return_value=Path('/fixture/codex')), \
                            patch('desktop_launcher.DesktopBridge') as bridge, \
                            patch('desktop_launcher.relay') as relay:
                        probe = bridge.return_value.__enter__.return_value
                        probe.request.return_value = reply
                        if permitted:
                            self.assertEqual(desktop_launcher.main(args), 0)
                            relay.assert_called_once()
                        else:
                            with self.assertRaises(ValueError):
                                desktop_launcher.main(args)
                            relay.assert_not_called()
                        probe.request.assert_called_once_with('config/read', {'includeLayers': False})

    def test_unknown_global_option_never_opens_bridge_or_relays(self):
        with patch('desktop_launcher.connection', return_value=Mock()), \
                patch('desktop_launcher.load', return_value=dict(mode='prepared')), \
                patch('desktop_launcher.validate_executable', return_value=Path('/fixture/codex')), \
                patch('desktop_launcher.DesktopBridge') as bridge, \
                patch('desktop_launcher.relay') as relay:
            with self.assertRaises(ValueError):
                desktop_launcher.main(['-c', 'approval_policy="never"', 'app-server'])
            bridge.assert_not_called()
            relay.assert_not_called()

    def test_unknown_server_config_never_relays(self):
        for reply in (None, [], {}, {'config': None}, {'config': []},
                      {'config': {}}, {'config': {'features': None}},
                      {'config': {'features': []}}):
            with self.subTest(reply=reply):
                with patch('desktop_launcher.connection', return_value=Mock()), \
                        patch('desktop_launcher.load', return_value=dict(mode='prepared')), \
                        patch('desktop_launcher.validate_executable', return_value=Path('/fixture/codex')), \
                        patch('desktop_launcher.DesktopBridge') as bridge, \
                        patch('desktop_launcher.relay') as relay:
                    bridge.return_value.__enter__.return_value.request.return_value = reply
                    with self.assertRaises(ValueError):
                        desktop_launcher.main(['app-server', '-c', 'features.windows_sandbox_service=true'])
                    relay.assert_not_called()
