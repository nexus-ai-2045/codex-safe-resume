import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == 'nt' and shutil.which('pwsh'), 'Windows PowerShell 7 shortcuts')
class ShortcutTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        fixtures = self.root.parent/'owned-fixture-temp'
        fixtures.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=fixtures)
        self.addCleanup(self.temp.cleanup)

    def verify(self, script, change='', owner_fixture=True, fixture_path=None):
        ownership = '''
function Get-Acl {
    $value=[pscustomobject]@{}
    $value | Add-Member -MemberType ScriptMethod -Name GetOwner -Value {
        param($type)
        [Security.Principal.WindowsIdentity]::GetCurrent().User
    }
    return $value
}
''' if owner_fixture else ''
        command = '''
. $env:CSR_COMMON
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:CSR_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw '構文エラー' }
$function=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-CsrOwnedShortcut'},$true)
if ($null -eq $function) { throw '検証関数がありません' }
Invoke-Expression $function.Extent.Text
$path=Join-Path $env:CSR_FIXTURE 'owned.lnk'
$target=Join-Path $env:CSR_FIXTURE 'CodexSafeResume.exe'
[IO.File]::WriteAllText($target,'inert fixture')
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($path)
$link.TargetPath=$target;$link.Arguments='desktop';$link.Save()
$aclBefore=(Get-Acl -LiteralPath $path).Sddl
$actualOwner=(Get-Acl -LiteralPath $path).GetOwner([Security.Principal.SecurityIdentifier]).Value
Write-Output ('fixture_owner_matches=' + ($actualOwner -eq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value))
''' + ownership + change + '''
try {
Assert-CsrOwnedShortcut $path $target 'desktop'
} finally {
    if ((Microsoft.PowerShell.Security\\Get-Acl -LiteralPath $path).Sddl -cne $aclBefore) { throw '試験で所有権またはACLが変化しました。' }
    Write-Output 'fixture_acl_unchanged=True'
}
'''
        return subprocess.run([shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command', command],
                              env=dict(os.environ, CSR_COMMON=str(self.root/'packaging/windows-common.ps1'),
                                       CSR_SCRIPT=str(self.root/'packaging'/script), CSR_FIXTURE=fixture_path or self.temp.name),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def test_matching_owned_shortcut_is_accepted_by_both_scripts(self):
        for script in ('install-windows.ps1', 'uninstall-windows.ps1'):
            with self.subTest(script=script):
                result = self.verify(script)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_native_fixture_ownership_is_enforced_without_acl_changes(self):
        for script in ('install-windows.ps1', 'uninstall-windows.ps1'):
            with self.subTest(script=script):
                result = self.verify(script, owner_fixture=False)
                expected = b'fixture_owner_matches=True' in result.stdout
                self.assertEqual(result.returncode == 0, expected, result.stderr.decode(errors='replace'))
                self.assertIn(b'fixture_acl_unchanged=True', result.stdout)
                self.assertTrue((Path(self.temp.name)/'owned.lnk').is_file())

    def test_native_short_path_fixture_keeps_shortcut_target_validation(self):
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(self.temp.name, buffer, len(buffer))
        self.assertGreater(length, 0)
        self.assertLess(length, len(buffer))
        self.assertTrue(os.path.samefile(self.temp.name, buffer.value))
        for script in ('install-windows.ps1', 'uninstall-windows.ps1'):
            with self.subTest(script=script):
                result = self.verify(script, fixture_path=buffer.value)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_changed_arguments_or_target_are_rejected_and_preserved(self):
        for script in ('install-windows.ps1', 'uninstall-windows.ps1'):
            for change in ("$link.Arguments='gui';$link.Save()", "$link.TargetPath=$target+'.other';$link.Save()"):
                with self.subTest(script=script, change=change):
                    result = self.verify(script, change)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue((Path(self.temp.name)/'owned.lnk').is_file())

    def test_owner_mismatch_is_rejected_without_changing_shortcut_acl(self):
        change = '''
function Get-Acl {
    $value=[pscustomobject]@{}
    $value | Add-Member -MemberType ScriptMethod -Name GetOwner -Value {
        param($type)
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    }
    return $value
}
'''
        for script in ('install-windows.ps1', 'uninstall-windows.ps1'):
            with self.subTest(script=script):
                result = self.verify(script, change)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((Path(self.temp.name)/'owned.lnk').is_file())

    def test_publish_refuses_existing_link_without_overwriting_its_content(self):
        command = '''
. $env:CSR_COMMON
$source=Join-Path $env:CSR_FIXTURE 'new.lnk'
$destination=Join-Path $env:CSR_FIXTURE 'existing.lnk'
[IO.File]::WriteAllText($source,'new owned fixture')
[IO.File]::WriteAllText($destination,'existing owned fixture')
try { Publish-CsrShortcut $source $destination; exit 2 } catch {}
if ([IO.File]::ReadAllText($destination) -cne 'existing owned fixture') { exit 3 }
if ([IO.File]::ReadAllText($source) -cne 'new owned fixture') { exit 4 }
exit 0
'''
        result = subprocess.run([shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command', command],
                                env=dict(os.environ, CSR_COMMON=str(self.root/'packaging/windows-common.ps1'),
                                         CSR_FIXTURE=self.temp.name),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

    def test_installer_offers_only_safe_resume_gui_and_retains_legacy_cleanup(self):
        install = (self.root/'packaging/install-windows.ps1').read_text(encoding='utf-8')
        uninstall = (self.root/'packaging/uninstall-windows.ps1').read_text(encoding='utf-8')
        self.assertNotIn('DesktopShortcut', install)
        self.assertNotIn('Codex（Safe Resume接続）.lnk', install)
        self.assertNotIn("Arguments='desktop'", install)
        self.assertIn("Arguments='gui'", install)
        self.assertIn('Codex（Safe Resume接続）.lnk', uninstall)
        self.assertIn("Assert-CsrOwnedShortcut $desktopShortcutPath $exe 'desktop'", uninstall)
        self.assertLess(uninstall.index("Assert-CsrOwnedShortcut $desktopShortcutPath $exe 'desktop'"),
                        uninstall.index('Remove-Item -LiteralPath $shortcut'))

    def test_windows_documentation_does_not_offer_custom_codex_startup(self):
        for name in ('README.md', 'docs/接続設定.md', 'packaging/README-portable-ja.md'):
            with self.subTest(name=name):
                content = (self.root/name).read_text(encoding='utf-8')
                self.assertNotIn('-DesktopShortcut', content)
                self.assertNotIn('CodexSafeResume.exe desktop', content)
                self.assertIn('共通方式', content)

    def test_both_os_documentation_withdraws_restart_based_connection(self):
        for name in ('README.md', 'docs/接続設定.md', 'packaging/README-portable-ja.md'):
            with self.subTest(name=name):
                content = (self.root/name).read_text(encoding='utf-8')
                self.assertIn('起動済み', content)
                self.assertNotIn('CODEX_CLI_PATH', content)
                self.assertNotIn('CODEX_APP_SERVER_FORCE_CLI', content)
                self.assertNotIn('ChatGPTを開き直して接続を修復', content)

    def test_connection_contracts_do_not_require_desktop_restart(self):
        safety = (self.root/'docs/再開の安全契約.md').read_text(encoding='utf-8')
        windows = (self.root/'docs/Windows接続の検証.md').read_text(encoding='utf-8')
        self.assertNotIn('既存の公式`CLI`と`Desktop`を接続して開き直します', safety)
        self.assertNotIn('3分以内', windows)
        self.assertNotIn('Quit', windows)
        self.assertIn('再起動前提の自動接続は両', safety)
        self.assertIn('旧方式は廃止', windows)
        self.assertIn('thread/queue/add', windows)

    def test_optional_switch_is_never_assigned_as_case_insensitive_local_variable(self):
        command = '''
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:CSR_SCRIPT,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw '構文エラー' }
$assignments=$ast.FindAll({param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
    $node.Left.VariablePath.UserPath -ieq 'DesktopShortcut'
},$true)
if ($assignments.Count -ne 0) { throw '任意指定スイッチへの代入を検出しました' }
'''
        for script in ('install-windows.ps1', 'uninstall-windows.ps1'):
            with self.subTest(script=script):
                result = subprocess.run(
                    [shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-Command', command],
                    env=dict(os.environ, CSR_SCRIPT=str(self.root/'packaging'/script)),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))


if __name__ == '__main__':
    unittest.main()
