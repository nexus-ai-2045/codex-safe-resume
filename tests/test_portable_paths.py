"""実ユーザー領域を使わずportable配置の所有権・リンク拒否を検証する。"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
        self.assertNotEqual(self.call('csr_remove_settings "$2"',state).returncode,0)
        self.assertTrue((state/'resume.sqlite3').exists())
        self.assertTrue((state/'sentinel').exists())
    def test_known_settings_removed_only_when_explicit(self):
        state=self.root/'state'; state.mkdir();(state/'resume.sqlite3').write_text('db')
        self.assertEqual(self.call('csr_remove_settings "$2"',state).returncode,0)
        self.assertFalse(state.exists())
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

if __name__=='__main__': unittest.main()
