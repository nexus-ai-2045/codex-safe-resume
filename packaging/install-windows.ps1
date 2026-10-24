# PowerShell 7 で実行。ユーザー領域のportable配置だけを扱います。
param([string]$Target = (Join-Path $env:LOCALAPPDATA 'Programs\CodexSafeResume'))
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
$shortcutTemp=$null; $stage=$null; $installed=$false; $linked=$false; $lock=$null; $success=$false
try {
    . (Join-Path $PSScriptRoot 'windows-common.ps1')
    if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7以降で実行してください。' }
    $source=Join-Path $PSScriptRoot 'CodexSafeResume'
    $expected=[IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\CodexSafeResume'))
    $targetFull=[IO.Path]::GetFullPath($Target)
    if ($targetFull -ne $expected) { throw '既定のユーザー専用配置先以外は使用できません。' }
    $desktop=[Environment]::GetFolderPath('DesktopDirectory')
    $shortcut=Join-Path $desktop 'Codex Safe Resume.lnk'
    Assert-CsrSafePath $source; Assert-CsrSafePath $targetFull; Assert-CsrSafePath $shortcut
    if (-not (Test-Path -LiteralPath (Join-Path $source 'CodexSafeResume.exe') -PathType Leaf)) { throw 'ビルド済みアプリが見つかりません。' }
    if ((Test-Path -LiteralPath $targetFull) -or (Test-Path -LiteralPath $shortcut)) { throw '既存の配置先または起動アイコンを上書きしません。' }
    @(Get-CsrManifest $source) | Out-Null
    $parent=Split-Path -Path $targetFull -Parent
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $lockPath=Join-Path $parent '.codex-safe-resume-install.lock'
    Assert-CsrSafePath $lockPath
    $lock=[IO.File]::Open($lockPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $stage=Join-Path $parent ('.codex-safe-resume-stage.'+[Guid]::NewGuid().ToString('N'))
    Copy-Item -LiteralPath $source -Destination $stage -Recurse
    Write-CsrOwner $stage
    [IO.Directory]::Move($stage,$targetFull); $shortcutTemp=$null; $stage=$null; $installed=$true
    $shell=New-Object -ComObject WScript.Shell
    $shortcutTemp=Join-Path $desktop ('.codex-safe-resume.'+[Guid]::NewGuid().ToString('N')+'.lnk')
    $link=$shell.CreateShortcut($shortcutTemp)
    $link.TargetPath=Join-Path $targetFull 'CodexSafeResume.exe'
    $link.Arguments='gui';$link.WorkingDirectory=$targetFull
    $link.Description='Codex 自動再開管理（安全モード）'
    $link.Save()
    Assert-CsrOwnedShortcut $shortcutTemp $link.TargetPath 'gui'
    $shortcutHash=(Get-FileHash -LiteralPath $shortcutTemp -Algorithm SHA256).Hash
    Publish-CsrShortcut $shortcutTemp $shortcut; $shortcutTemp=$null; $linked=$true
    Assert-CsrOwner $targetFull
    $success=$true

} catch {
    [Console]::Error.WriteLine('配置を完了できません。既存ファイル・権限・リンク・PowerShellの版を確認してください。')
} finally {
    if (-not $success) {
        if ($linked) {
            try {
                Assert-CsrOwnedShortcut $shortcut (Join-Path $targetFull 'CodexSafeResume.exe') 'gui'
                if ((Get-FileHash -LiteralPath $shortcut -Algorithm SHA256).Hash -cne $shortcutHash) { throw '起動アイコンが変更されました。' }
                Remove-Item -LiteralPath $shortcut -Force
            }
            catch { [Console]::Error.WriteLine('起動アイコンの確認が必要です。') }
        }
        if ($installed) {
            try { Assert-CsrOwner $targetFull; Remove-Item -LiteralPath $targetFull -Recurse -Force } catch { [Console]::Error.WriteLine('変更された配置先を保持しました。') }
        }
    }
    if ($stage) {
        try { @(Get-CsrManifest $stage) | Out-Null; Remove-Item -LiteralPath $stage -Recurse -Force }
        catch { [Console]::Error.WriteLine('削除できない一時配置を保持しました。手動確認が必要です。') }
    }
    if ($shortcutTemp) {
        try { Assert-CsrSafePath $shortcutTemp; Remove-Item -LiteralPath $shortcutTemp -Force -ErrorAction Stop }
        catch { [Console]::Error.WriteLine('一時アイコンの確認が必要です。') }
    }
    if ($lock) {
        try { $lock.Dispose(); Assert-CsrSafePath $lockPath; Remove-Item -LiteralPath $lockPath -Force }
        catch { $success=$false; [Console]::Error.WriteLine('配置ロックの確認が必要です。') }
    }
}
if (-not $success) { exit 1 }

Write-Host 'ユーザー領域への配置と起動アイコン作成を確認しました。'
Write-Host 'レジストリとログイン時の自動起動は変更していません。'
