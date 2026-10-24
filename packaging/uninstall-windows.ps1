param([switch]$RemoveUserSettings)
$ErrorActionPreference='Stop'
function Assert-CsrOwnedShortcut([string]$Path, [string]$Executable, [string]$Arguments) {
    Assert-CsrSafePath $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw '起動アイコンの実体を確認できません。' }
    $owner=(Get-Acl -LiteralPath $Path).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { throw '別の所有者の起動アイコンを保持しました。' }
    $reader=New-Object -ComObject WScript.Shell
    $candidate=$reader.CreateShortcut($Path)
    if ([IO.Path]::GetFullPath($candidate.TargetPath) -ne [IO.Path]::GetFullPath($Executable) -or
        $candidate.Arguments -cne $Arguments) { throw '別の起動アイコンを保持しました。' }
}
$lock=$null;$success=$false
try {
    . (Join-Path $PSScriptRoot 'windows-common.ps1')
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7以降で実行してください。' }
    $target=Join-Path $env:LOCALAPPDATA 'Programs\CodexSafeResume'
    $exe=Join-Path $target 'CodexSafeResume.exe'
    $shortcut=Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Codex Safe Resume.lnk'
    $desktopShortcutPath=Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'Codex（Safe Resume接続）.lnk'
    Assert-CsrSafePath $target;Assert-CsrSafePath $shortcut;Assert-CsrSafePath $desktopShortcutPath
    if (Get-Process -Name CodexSafeResume -ErrorAction SilentlyContinue) { throw 'アプリを終了してください。' }
    if (Test-Path -LiteralPath $target) {
        $lockPath=Join-Path (Split-Path -Path $target -Parent) '.codex-safe-resume-install.lock'
        Assert-CsrSafePath $lockPath
        $lock=[IO.File]::Open($lockPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        Assert-CsrOwner $target
        if (Test-Path -LiteralPath $shortcut) {
            Assert-CsrOwnedShortcut $shortcut $exe 'gui'
        }
        if (Test-Path -LiteralPath $desktopShortcutPath) {
            Assert-CsrOwnedShortcut $desktopShortcutPath $exe 'desktop'
        }
        if (Test-Path -LiteralPath $shortcut) {
            Remove-Item -LiteralPath $shortcut -Force
        }
        if (Test-Path -LiteralPath $desktopShortcutPath) {
            Remove-Item -LiteralPath $desktopShortcutPath -Force
        }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
    if ($RemoveUserSettings) { Remove-CsrSettings (Join-Path $HOME '.codex-safe-resume') }
    $success=$true

} catch {
    [Console]::Error.WriteLine('削除を停止しました。アプリ終了・所有記録・リンク・配置後の変更を確認してください。')
} finally {
    if ($lock) {
        try { $lock.Dispose(); Assert-CsrSafePath $lockPath; Remove-Item -LiteralPath $lockPath -Force }
        catch { $success=$false; [Console]::Error.WriteLine('配置ロックを保持しました。手動確認が必要です。') }
    }
}
if (-not $success) { exit 1 }

Write-Host 'アンインストールを確認しました。Codex本体と会話データは保持しました。'
