"""実ユーザー領域を使わずportable配置の所有権・リンク拒否を検証する。"""
from pathlib import Path
import hashlib
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin', 'macOSのシェル検証')
class MacPortable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
    def call(self, script, *args):
        return subprocess.run(['/bin/bash', '-c', 'set -euo pipefail; source "$1"; '+script,
                               'test', str(ROOT/'packaging/macos-common.sh'), *map(str,args)],
                              capture_output=True, text=True)
    def test_parent_symlink_refused(self):
        (self.root/'real').mkdir()
        (self.root/'alias').symlink_to(self.root/'real', target_is_directory=True)
        self.assertNotEqual(self.call('csr_safe_path "$2"', self.root/'alias'/'app').returncode, 0)
        self.assertEqual(self.call('csr_safe_path "$2"', self.root/'real'/'app').returncode, 0)
    def test_modified_or_extra_files_are_not_owned(self):
        app=self.root/'app'; app.mkdir(); (app/'payload').write_text('original')
        self.assertEqual(self.call('csr_write_owner "$2"; csr_verify_owner "$2"', app).returncode, 0)
        (app/'sentinel').write_text('keep')
        self.assertNotEqual(self.call('csr_verify_owner "$2"',app).returncode,0)
        self.assertTrue((app/'sentinel').exists())
        (app/'sentinel').unlink(); (app/'payload').write_text('changed')
        self.assertNotEqual(self.call('csr_verify_owner "$2"',app).returncode,0)
    def test_unmarked_app_not_owned(self):
        app=self.root/'app'; app.mkdir(); (app/'payload').write_text('keep')
        self.assertNotEqual(self.call('csr_verify_owner "$2"',app).returncode,0)
    def test_unexpected_settings_preserved(self):
        state=self.root/'state'; state.mkdir();(state/'resume.sqlite3').write_text('db');(state/'sentinel').write_text('keep')
        (state/'gui.lock').write_bytes(b'')
        self.assertNotEqual(self.call('csr_remove_settings "$2"',state).returncode,0)
        self.assertTrue((state/'resume.sqlite3').exists())
        self.assertTrue((state/'sentinel').exists())
        self.assertTrue((state/'gui.lock').exists())
    def test_known_settings_removed_only_when_explicit(self):
        state=self.root/'state'; state.mkdir();(state/'resume.sqlite3').write_text('db')
        self.assertEqual(self.call('csr_remove_settings "$2"',state).returncode,0)
        self.assertFalse(state.exists())
        lock = state.with_name(state.name + '.gui.lock')
        self.assertEqual(list(self.root.iterdir()), [lock])
        self.assertEqual(lock.read_bytes(), b'')

    def test_relative_uninstall_entry_removes_only_isolated_settings(self):
        portable = self.root/'portable'; portable.mkdir()
        for name in ('uninstall-macos.command', 'macos-common.sh'):
            shutil.copy2(ROOT/'packaging'/name, portable/name)
        home = self.root/'home'; home.mkdir(mode=0o700)
        (home/'Applications').mkdir(); (home/'Desktop').mkdir()
        state = home/'.codex-safe-resume'; state.mkdir(mode=0o700)
        (state/'resume.sqlite3').write_bytes(b'isolated settings')
        # This test concerns relative entry paths, independently of real apps.
        binary = self.root/'bin'; binary.mkdir()
        probe = binary/'pgrep'; probe.write_text('#!/bin/sh\nexit 1\n'); probe.chmod(0o700)
        env = dict(os.environ, HOME=str(home), PATH=str(binary)+os.pathsep+os.environ.get('PATH', ''))
        result = subprocess.run(['/bin/bash', './uninstall-macos.command', '--remove-settings'],
                                cwd=portable, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(state.exists())
        self.assertTrue((home/'.codex-safe-resume.gui.lock').is_file())

    def test_relative_helper_symlink_refuses_settings_removal(self):
        helper = self.root/'helper.sh'; helper.symlink_to(ROOT/'packaging/macos-common.sh')
        state = self.root/'state'; state.mkdir(mode=0o700)
        sentinel = state/'resume.sqlite3'; sentinel.write_bytes(b'keep')
        result = subprocess.run(['/bin/bash', '-c', 'source ./helper.sh; csr_remove_settings "$1"',
                                 'relative-helper', str(state)], cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b'keep')

    def test_active_gui_lease_prevents_settings_removal(self):
        from core import gui_lease
        state = self.root/'state'
        with gui_lease(state) as acquired:
            self.assertTrue(acquired)
            result = self.call('csr_remove_settings "$2"', state)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('使用中', result.stderr)
            self.assertTrue(state.with_name(state.name + '.gui.lock').exists())
        self.assertEqual(self.call('csr_remove_settings "$2"', state).returncode, 0)

    def test_removal_holds_same_sibling_lease_through_delete(self):
        from core import gui_lease
        state = self.root/'state with spaces'
        state.mkdir(mode=0o700)
        (state/'resume.sqlite3').write_bytes(b'isolated settings')
        # A copied helper pauses its real rm boundary without a production test hook.
        helper = self.root/'helper.sh'
        helper.write_text((ROOT/'packaging/macos-common.sh').read_text() + '''
rm() {
  printf 'deletion-boundary\\n'
  IFS= read -r release
  command /bin/rm "$@"
}
''')
        process = subprocess.Popen(['/bin/bash', '-c', 'source "$1"; csr_remove_settings "$2"',
                                    'test', str(helper), str(state)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        lines = queue.Queue()
        threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
        self.assertEqual(lines.get(timeout=10), 'deletion-boundary\n')
        try:
            lock = state.with_name(state.name + '.gui.lock')
            with gui_lease(state) as acquired:
                self.assertFalse(acquired, '削除処理中にGUIが同時起動してはならない')
            self.assertTrue(lock.exists())
            inode = lock.stat().st_ino
            self.assertEqual((state/'resume.sqlite3').read_bytes(), b'isolated settings')
        finally:
            _, error = process.communicate('\n', timeout=10)
        self.assertEqual(process.returncode, 0, error)
        self.assertFalse(state.exists())
        self.assertEqual(lock.stat().st_ino, inode)
        with gui_lease(state) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(lock.stat().st_ino, inode)

    def test_invalid_sibling_lock_preserves_all_settings(self):
        state = self.root/'state'
        state.mkdir(mode=0o700)
        (state/'resume.sqlite3').write_bytes(b'preserved')
        lock = self.root/'state.gui.lock'
        target = self.root/'target'
        target.write_bytes(b'keep')
        target.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'mode', 'oversize', 'directory'):
            with self.subTest(kind=kind):
                try:
                    if kind == 'symlink':
                        lock.symlink_to(target)
                    elif kind == 'hardlink':
                        os.link(target, lock)
                    elif kind == 'directory':
                        lock.mkdir()
                    else:
                        lock.write_bytes(b'xx' if kind == 'oversize' else b'')
                        lock.chmod(0o644 if kind == 'mode' else 0o600)
                    result = self.call('csr_remove_settings "$2"', state)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((state/'resume.sqlite3').read_bytes(), b'preserved')
                    self.assertEqual(target.read_bytes(), b'keep')
                finally:
                    if lock.is_dir() and not lock.is_symlink():
                        lock.rmdir()
                    else:
                        lock.unlink()

    def test_private_settings_helper_requires_inherited_lock(self):
        state = self.root/'state'
        state.mkdir(mode=0o700)
        (state/'resume.sqlite3').write_bytes(b'preserved')
        result = self.call('_csr_remove_settings_locked "$2"', state)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((state/'resume.sqlite3').read_bytes(), b'preserved')

    def test_removal_child_retains_lease_if_supervisor_exits(self):
        from core import gui_lease
        state = self.root/'state'
        state.mkdir(mode=0o700)
        (state/'resume.sqlite3').write_bytes(b'isolated')
        helper = self.root/'helper.sh'
        helper.write_text((ROOT/'packaging/macos-common.sh').read_text() + '''
rm() {
  printf '%s\\n' "$PPID"
  IFS= read -r release
  command /bin/rm "$@"
}
''')
        process = subprocess.Popen(['/bin/bash','-c','source "$1"; csr_remove_settings "$2"',
                                    'test',str(helper),str(state)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            lines = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            supervisor = int(lines.get(timeout=10).strip())
            # This PID is emitted by our own isolated deletion child, never a live app.
            os.kill(supervisor, signal.SIGTERM)
            with gui_lease(state) as acquired:
                self.assertFalse(acquired)
            process.communicate('\n', timeout=10)
            self.assertFalse(state.exists())
            with gui_lease(state) as acquired:
                self.assertTrue(acquired)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
    def test_manifest_uses_one_hash_worker_for_many_entries(self):
        app=self.root/'app';app.mkdir()
        for index in range(64):
            (app/f'payload {index:03}').write_bytes(bytes([index])*64)
        for index in range(6):
            (app/f'link {index}').symlink_to(f'payload {index:03}')
        result=self.call('set -x; csr_manifest "$2"',app)
        self.assertEqual(result.returncode,0,result.stderr)
        workers=re.findall(r'^\++ (?:/usr/bin/(?:shasum|perl)|readlink) ',result.stderr,re.MULTILINE)
        self.assertLessEqual(len(workers),1,'ハッシュ・リンク検査は単一のworkerで処理する')
        self.assertEqual(len(result.stdout.splitlines()),70)

    def test_owner_v1_manifest_matches_golden_bytes(self):
        app=self.root/'Golden App.app';app.mkdir()
        directory=app/'資料 folder';directory.mkdir()
        payload=b'payload\x00with binary\xff\n'
        (directory/'data space').write_bytes(payload)
        (app/'empty').write_bytes(b'')
        target='資料 folder/data space'
        (app/'relative link').symlink_to(target)
        (app/'directory link').symlink_to('資料 folder',target_is_directory=True)
        marker=app/'.custom-owner'
        expected=[
            'D\t-\t資料 folder',
            'F\t'+hashlib.sha256(payload).hexdigest()+'\t資料 folder/data space',
            'F\t'+hashlib.sha256(b'').hexdigest()+'\tempty',
            'L\t'+hashlib.sha256((target+'\n').encode()).hexdigest()+'\trelative link',
            'L\t'+hashlib.sha256('資料 folder\n'.encode()).hexdigest()+'\tdirectory link',
        ]
        golden=b'codex-safe-resume-portable-v1\n'+b''.join(
            line.encode()+b'\n' for line in sorted(expected,key=lambda line:line.encode()))
        result=self.call('csr_write_owner "$2" "$3"; csr_verify_owner "$2" "$3"',app,marker)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(marker.read_bytes(),golden)

    def test_manifest_refuses_control_paths_and_unknown_file_types(self):
        for character in ('\n','\r','\t'):
            with self.subTest(character=repr(character)):
                app=self.root/('app'+str(ord(character)));app.mkdir()
                (app/('bad'+character+'name')).write_bytes(b'keep')
                self.assertNotEqual(self.call('csr_manifest "$2"',app).returncode,0)
        app=self.root/'fifo-app';app.mkdir();os.mkfifo(app/'pipe')
        self.assertNotEqual(self.call('csr_manifest "$2"',app).returncode,0)

    def test_external_bundle_symlink_refused(self):
        app=self.root/'app';app.mkdir();outside=self.root/'outside';outside.write_text('keep')
        (app/'link').symlink_to(outside)
        self.assertNotEqual(self.call('csr_write_owner "$2"',app).returncode,0)
        self.assertEqual(outside.read_text(),'keep')

    def test_internal_bundle_symlinks_remain_owned_after_relocation(self):
        app=self.root/'Source App.app'
        version=app/'Contents/Frameworks/Versions/A';version.mkdir(parents=True)
        (version/'payload data').write_text('bundle payload')
        (version.parent/'Current').symlink_to('A',target_is_directory=True)
        resources=app/'Contents/Resources';resources.mkdir()
        (resources/'payload data').symlink_to('../Frameworks/Versions/Current/payload data')
        self.assertEqual(self.call('csr_write_owner "$2"; csr_verify_owner "$2"',app).returncode,0)
        relocated=self.root/'Installed App.app'
        shutil.copytree(app,relocated,symlinks=True)
        self.assertEqual(self.call('csr_verify_owner "$2"',relocated).returncode,0)

    def test_owner_marker_symlink_refused(self):
        app=self.root/'app';app.mkdir();sentinel=self.root/'sentinel';sentinel.write_text('keep')
        (app/'.codex-safe-resume-owner').symlink_to(sentinel)
        self.assertNotEqual(self.call('csr_write_owner "$2"',app).returncode,0)
        self.assertEqual(sentinel.read_text(),'keep')


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell実行環境が必要')
class WindowsPortable(unittest.TestCase):
    def test_reparse_parent_is_rejected_before_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();real=root/'real';real.mkdir();(real/'sentinel').write_text('keep')
            alias=root/'alias'
            import os
            if os.name == 'nt':
                made=subprocess.run(['cmd','/c','mklink','/J',str(alias),str(real)],capture_output=True)
                self.assertEqual(made.returncode,0)
            else:
                alias.symlink_to(real,target_is_directory=True)
            probe=root/'probe.ps1';probe.write_text('param([string]$Helper,[string]$Target)\n. $Helper; Assert-CsrSafePath $Target')
            helper=str(ROOT/'packaging/windows-common.ps1')
            valid=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(probe),helper,str(real/'child')],capture_output=True,text=True,encoding='utf-8')
            invalid=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(probe),helper,str(alias/'child')],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(valid.returncode,0)
            self.assertNotEqual(invalid.returncode,0)
            self.assertEqual((real/'sentinel').read_text(),'keep')

    def test_atomic_shortcut_publish_preserves_competing_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();candidate=root/'temporary.lnk';existing=root/'existing.lnk'
            candidate.write_text('new'); existing.write_text('keep')
            probe=root/'probe.ps1';probe.write_text('param([string]$Helper,[string]$Candidate,[string]$Existing)\n. $Helper; Publish-CsrShortcut $Candidate $Existing')
            result=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(probe),
                                  str(ROOT/'packaging/windows-common.ps1'),str(candidate),str(existing)],capture_output=True,text=True,encoding='utf-8')
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(existing.read_text(),'keep')
            self.assertEqual(candidate.read_text(),'new')

    def test_manifest_rejects_additional_files(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();app=root/'app';app.mkdir();(app/'payload').write_text('keep')
            probe=root/'probe.ps1'
            probe.write_text("param([string]$Helper,[string]$Root)\n. $Helper; Write-CsrOwner $Root; Assert-CsrOwner $Root; Set-Content (Join-Path $Root sentinel) keep; Assert-CsrOwner $Root")
            result=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(probe),
                                   str(ROOT/'packaging/windows-common.ps1'),str(app)],capture_output=True,text=True,encoding='utf-8')
            self.assertNotEqual(result.returncode,0)
            self.assertTrue((app/'sentinel').exists(), '所有権検査を実行する前に失敗していないこと')

@unittest.skipUnless(sys.platform == 'win32' and shutil.which('pwsh'),
                     'PythonとPowerShellのWindows native共有拒否の検証が必要')
class WindowsGuiRemovalInterop(unittest.TestCase):
    def test_python_gui_and_native_removal_mutually_exclude(self):
        from core import gui_lease
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state = root/'state with spaces'
            probe = root/'probe.ps1'
            probe.write_text('''param([string]$Helper, [string]$Root, [switch]$Pause)
. $Helper
if ($Pause) {
    $original = (Get-Command _Remove-CsrSettingsLocked).ScriptBlock
    function _Remove-CsrSettingsLocked([string]$Root, [IO.FileStream]$GuiLease) {
        [Console]::Out.WriteLine('deletion-boundary')
        [void][Console]::In.ReadLine()
        & $original $Root $GuiLease
    }
}
Remove-CsrSettings $Root
''')
            command = ['pwsh', '-NoProfile', '-NonInteractive', '-File', str(probe),
                       str(ROOT/'packaging/windows-common.ps1'), str(state)]
            with gui_lease(state) as acquired:
                self.assertTrue(acquired)
                (state/'resume.sqlite3').write_bytes(b'isolated settings')
                before = {p.name: p.read_bytes() for p in state.iterdir()}
                result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual({p.name: p.read_bytes() for p in state.iterdir()}, before)
            lock = state.with_name(state.name + '.gui.lock')
            inode = lock.stat().st_ino
            process = subprocess.Popen(command+['-Pause'], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                lines = queue.Queue()
                threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
                self.assertEqual(lines.get(timeout=20), 'deletion-boundary\n')
                with gui_lease(state) as acquired:
                    self.assertFalse(acquired)
                _, error = process.communicate('\n', timeout=20)
                self.assertEqual(process.returncode, 0, error)
                self.assertFalse(state.exists())
                self.assertEqual(lock.stat().st_ino, inode)
                with gui_lease(state) as acquired:
                    self.assertTrue(acquired)
                    self.assertEqual(lock.stat().st_ino, inode)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_unknown_settings_preserve_every_file(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'state'
            state.mkdir()
            (state/'resume.sqlite3').write_bytes(b'fixture')
            (state/'sentinel').write_bytes(b'keep')
            probe = Path(directory)/'probe.ps1'
            probe.write_text('param([string]$Helper,[string]$Root)\n. $Helper; Remove-CsrSettings $Root')
            result = subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(probe),
                                     str(ROOT/'packaging/windows-common.ps1'),str(state)],
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((state/'resume.sqlite3').read_bytes(), b'fixture')
            self.assertEqual((state/'sentinel').read_bytes(), b'keep')


if __name__=='__main__': unittest.main()
