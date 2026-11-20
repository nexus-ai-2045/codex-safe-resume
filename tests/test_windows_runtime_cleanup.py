import hashlib
import json
import os
import stat
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest


@unittest.skipUnless(os.name == 'nt' and shutil.which('pwsh'), 'Windows PowerShell 7 cleanup')
class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='csr-cleanup-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'state'
        self.runtime = self.home / 'runtime' / ('a' * 64)
        self.runtime.mkdir(parents=True)
        (self.home / 'resume.sqlite3').write_bytes(b'owned settings')
        names = ('codex.exe', 'codex-code-mode-host.exe', 'codex-command-runner.exe',
                 'codex-windows-sandbox-setup.exe')
        self.files = {}
        for name in names:
            payload = ('inert ' + name).encode()
            (self.runtime / name).write_bytes(payload)
            self.files[name] = hashlib.sha256(payload).hexdigest()
        self.marker = self.runtime / '.codex-safe-resume-runtime.json'
        self.write_marker()

    def write_marker(self):
        self.marker.write_text(json.dumps(dict(schema='codex-safe-resume.runtime/v1',
                                               key=self.runtime.name, files=self.files)), encoding='utf-8')

    def snapshot(self):
        return {str(path.relative_to(self.home)): path.read_bytes()
                for path in self.home.rglob('*') if path.is_file()}

    def remove(self, prefix=''):
        common = Path(__file__).resolve().parents[1] / 'packaging' / 'windows-common.ps1'
        return subprocess.run(
            [shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command',
             '. $env:CSR_TEST_COMMON; ' + prefix + 'Remove-CsrSettings $env:CSR_TEST_SETTINGS'],
            env=dict(os.environ, CSR_TEST_COMMON=str(common), CSR_TEST_SETTINGS=str(self.home),
                     CSR_TEST_LOCKED=str(self.runtime / 'codex.exe')),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def assert_preserved(self):
        before = self.snapshot()
        result = self.remove()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_exact_manifest_allows_explicit_removal(self):
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertFalse(self.home.exists())

    def test_optional_known_service_is_removed(self):
        name = 'codex-windows-sandbox-service.exe'
        payload = b'inert optional service'
        (self.runtime / name).write_bytes(payload)
        self.files[name] = hashlib.sha256(payload).hexdigest()
        self.write_marker()
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertFalse(self.home.exists())

    def test_unknown_runtime_file_preserves_entire_tree(self):
        (self.runtime / 'unknown.exe').write_bytes(b'keep')
        self.assert_preserved()

    def test_unknown_settings_file_preserves_entire_tree(self):
        (self.home / 'unknown.txt').write_bytes(b'keep')
        self.assert_preserved()

    def test_missing_marker_preserves_entire_tree(self):
        self.marker.unlink()
        self.assert_preserved()

    def test_existing_settings_without_runtime_are_removed(self):
        for path in self.runtime.iterdir():
            path.unlink()
        self.runtime.rmdir()
        self.runtime.parent.rmdir()
        result = self.remove()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertFalse(self.home.exists())

    def test_changed_hash_preserves_entire_tree(self):
        (self.runtime / 'codex.exe').write_bytes(b'changed')
        self.assert_preserved()

    def test_unknown_settings_and_staging_directories_preserve_tree(self):
        for path in (self.home / 'unknown', self.home / 'runtime' / 'stage.failed'):
            with self.subTest(path=path.name):
                path.mkdir()
                self.assert_preserved()
                path.rmdir()

    def test_invalid_marker_preserves_entire_tree(self):
        original = self.marker.read_text(encoding='utf-8')
        for marker in ('{', original.replace('runtime/v1', 'runtime/v2'),
                       original.replace('a' * 64, 'b' * 64),
                       original.replace('codex.exe', '../codex.exe')):
            with self.subTest(marker=marker[:20]):
                self.marker.write_text(marker, encoding='utf-8')
                self.assert_preserved()

    def test_in_use_runtime_preserves_settings_and_runtime(self):
        before = self.snapshot()
        result = self.remove("$held = [IO.File]::Open($env:CSR_TEST_LOCKED, 'Open', 'Read', 'None'); ")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_in_use_settings_preserve_settings_and_runtime(self):
        before = self.snapshot()
        result = self.remove("$held = [IO.File]::Open((Join-Path $env:CSR_TEST_SETTINGS 'resume.sqlite3'), 'Open', 'Read', 'None'); ")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_junction_preserves_settings_and_external_target(self):
        target = Path(self.temp.name) / 'outside'
        target.mkdir()
        sentinel = target / 'keep'
        sentinel.write_bytes(b'owned external fixture')
        link = self.runtime / 'linked'
        created = subprocess.run(
            [shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command',
             "New-Item -ItemType Junction -Path $env:CSR_TEST_LINK -Target $env:CSR_TEST_TARGET | Out-Null"],
            env=dict(os.environ, CSR_TEST_LINK=str(link), CSR_TEST_TARGET=str(target)),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(created.returncode, 0, created.stderr.decode(errors='replace'))
        try:
            self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            self.assert_preserved()
            self.assertEqual(sentinel.read_bytes(), b'owned external fixture')
        finally:
            link.rmdir()

    def test_locked_runtime_cannot_be_replaced_before_deletion(self):
        ready = Path(self.temp.name) / 'locks-ready'
        release = Path(self.temp.name) / 'release-locks'
        prefix = '''
function ConvertFrom-Json {
    param([Parameter(ValueFromPipeline=$true)]$InputObject, [switch]$AsHashtable)
    process {
        [IO.File]::WriteAllText((Join-Path (Split-Path $env:CSR_TEST_SETTINGS) 'locks-ready'), 'ready')
        $release = Join-Path (Split-Path $env:CSR_TEST_SETTINGS) 'release-locks'
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not [IO.File]::Exists($release)) {
            if ([DateTime]::UtcNow -gt $deadline) { throw '試験の同期が期限切れです。' }
            Start-Sleep -Milliseconds 10
        }
        Microsoft.PowerShell.Utility\\ConvertFrom-Json -InputObject $InputObject -AsHashtable:$AsHashtable
    }
}
'''
        target = self.runtime / 'codex-windows-sandbox-setup.exe'
        retained = Path(self.temp.name) / 'retained.exe'
        results = []
        worker = threading.Thread(target=lambda: results.append(self.remove(prefix)))
        worker.start()
        locked = False
        swapped = False
        blocked_directories = []
        deadline = time.monotonic() + 12
        while worker.is_alive() and not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            self.assertTrue(ready.exists(), results[0].stderr.decode(errors='replace') if results else '同期未完了')
            try:
                with target.open('ab'):
                    pass
            except PermissionError:
                locked = True
                for directory in (self.home, self.runtime.parent, self.runtime):
                    relocated = directory.with_name(directory.name + '.relocated')
                    try:
                        directory.rename(relocated)
                    except PermissionError:
                        blocked_directories.append(True)
                    else:
                        blocked_directories.append(False)
                        relocated.rename(directory)
                try:
                    target.rename(retained)
                except PermissionError:
                    pass
                else:
                    target.write_bytes(b'unknown replacement')
                    swapped = True
        finally:
            release.write_bytes(b'release')
            worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertTrue(locked)
        self.assertEqual(blocked_directories, [True, True, True])
        self.assertEqual(len(results), 1)
        if swapped:
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b'unknown replacement')
            self.assertNotEqual(results[0].returncode, 0)
        else:
            self.assertEqual(results[0].returncode, 0, results[0].stderr.decode(errors='replace'))
