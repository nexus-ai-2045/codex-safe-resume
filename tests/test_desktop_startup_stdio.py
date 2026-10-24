import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BOOTSTRAP = '''import json
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, sys.argv[1])
import desktop_launcher

arguments = json.loads(sys.argv[2])
scenario = sys.argv[3]
config = dict(mode='managed', launch_nonce='isolated-startup-fixture')
os.environ['CSR_DESKTOP_NONCE'] = config['launch_nonce']
bridge = MagicMock()
if scenario == 'transport_failure':
    bridge.return_value.__enter__.side_effect = OSError('isolated-transport')
else:
    bridge.return_value.__enter__.return_value.request.return_value = {
        'config': {'features': {'prefer_mxc': False}}}
with patch('desktop_launcher.connection', return_value=Mock()), \\
        patch('desktop_launcher.load', return_value=config), \\
        patch('desktop_launcher.validate_executable', return_value=Path(sys._base_executable)), \\
        patch('desktop_binding.validate_startup_caller'), \\
        patch('desktop_binding.record_startup_fallback') as record, \\
        patch('desktop_launcher.DesktopBridge', bridge):
    result = desktop_launcher.main(arguments)
    reason = {'unsupported': 'unsupported_launch_options',
              'transport_failure': 'shared_transport_unavailable',
              'settings_mismatch': 'shared_settings_mismatch'}[scenario]
    record.assert_called_once_with(config, reason)
    raise SystemExit(result)
'''


FIXTURE_SERVER = '''import json
import queue
import sys
import threading

incoming = queue.Queue()
threading.Thread(target=lambda: incoming.put(sys.stdin.readline()), daemon=True).start()
request = json.loads(incoming.get(timeout=3))
print(json.dumps({'id': request['id'], 'result': {'fixture': 'initialized'}}), flush=True)
print(json.dumps({'argv': sys.argv[1:]}), flush=True)
print('isolated-cli-stderr', file=sys.stderr, flush=True)
exit_code = int(sys.argv[sys.argv.index('--fixture-exit') + 1]) if '--fixture-exit' in sys.argv else 0
raise SystemExit(exit_code)
'''


class DesktopStartupStdioIntegration(unittest.TestCase):
    def run_fallback(self, scenario, arguments, returncode):
        repository = Path(__file__).resolve().parents[1]
        fixture_root = repository.parent / 'owned-fixture-temp'
        fixture_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='startup-stdio-', dir=fixture_root) as directory:
            fixture = Path(directory)
            (fixture / 'bootstrap.py').write_text(BOOTSTRAP, encoding='utf-8')
            (fixture / 'app-server').write_text(FIXTURE_SERVER, encoding='utf-8')
            environment = os.environ.copy()
            environment.pop('PYTHONPATH', None)
            environment['PYTHONIOENCODING'] = 'utf-8'
            environment['PYTHONUTF8'] = '1'
            result = subprocess.run(
                [sys.executable, str(fixture / 'bootstrap.py'), str(repository),
                 json.dumps(arguments), scenario],
                cwd=fixture, env=environment,
                input=json.dumps({'id': 317, 'method': 'initialize', 'params': {}}) + '\n',
                capture_output=True, text=True, encoding='utf-8', timeout=20, check=False,
            )
        self.assertEqual(result.returncode, returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertEqual(json.loads(lines[0]), {
            'id': 317, 'result': {'fixture': 'initialized'}})
        self.assertEqual(json.loads(lines[1]), {'argv': arguments[1:]})
        self.assertEqual(result.stderr, 'isolated-cli-stderr\n')

    def test_unknown_future_options_preserve_stdio_and_exact_arguments(self):
        self.run_fallback('unsupported', [
            'app-server', '--analytics-default-enabled',
            '-c', 'features.future_desktop_feature=true',
            '--plugin', 'fixture plugin path', '--fixture-exit', '0'], 0)

    def test_unknown_future_options_preserve_nonzero_cli_exit(self):
        self.run_fallback('unsupported', [
            'app-server', '-c', 'features.future_desktop_feature=true',
            '--fixture-exit', '7'], 7)

    def test_transport_failure_runs_official_stdio_child(self):
        self.run_fallback('transport_failure', [
            'app-server', '-c', 'features.prefer_mxc=true'], 0)

    def test_settings_mismatch_runs_official_stdio_child(self):
        self.run_fallback('settings_mismatch', [
            'app-server', '-c', 'features.prefer_mxc=true'], 0)
