$ErrorActionPreference = 'Stop'
function Assert-CsrSafePath([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction SilentlyContinue
        if ($null -ne $item) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'リンクまたは接合点を経由する対象は操作しません。'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}
function Get-CsrManifest([string]$Root) {
    Assert-CsrSafePath $Root
    $queue = [Collections.Generic.Queue[string]]::new()
    $queue.Enqueue([IO.Path]::GetFullPath($Root))
    $entries = [Collections.Generic.List[string]]::new()
    while ($queue.Count -gt 0) {
        foreach ($entry in Get-ChildItem -LiteralPath $queue.Dequeue() -Force) {
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw '配布物内のリンクまたは接合点は使用できません。'
            }
            $relative = [IO.Path]::GetRelativePath($Root, $entry.FullName)
            if ($relative -eq '.codex-safe-resume-owner.json') { continue }
            if ($relative -match '[\x00-\x1f]') { throw '配布物内の名前を確認できません。' }
            if ($entry.PSIsContainer) {
                $entries.Add('D|' + $relative)
                $queue.Enqueue($entry.FullName)
            } else {
                $hash = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash
                $entries.Add('F|' + $hash + '|' + $relative)
            }
        }
    }
    return @($entries | Sort-Object -CaseSensitive)
}
function Write-CsrOwner([string]$Root) {
    $marker = Join-Path $Root '.codex-safe-resume-owner.json'
    Assert-CsrSafePath $marker
    if (Test-Path -LiteralPath $marker) { throw '既存の所有記録を上書きしません。' }
    $manifest = @{schema='codex-safe-resume-portable-v1'; entries=@(Get-CsrManifest $Root)} | ConvertTo-Json -Depth 3 -Compress
    $stream = [IO.File]::Open($marker, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($manifest)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
}
function Assert-CsrOwner([string]$Root) {
    $marker = Join-Path $Root '.codex-safe-resume-owner.json'
    Assert-CsrSafePath $marker
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { throw '配置の所有記録がありません。削除を停止しました。' }
    $saved = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($saved.schema -ne 'codex-safe-resume-portable-v1') { throw '配置の所有記録が不正です。' }
    $actual = @(Get-CsrManifest $Root)
    if (($saved.entries | ConvertTo-Json -Compress) -cne ($actual | ConvertTo-Json -Compress)) {
        throw '配置後の変更を検出しました。削除を停止しました。'
    }
}
function Initialize-CsrVerifiedRemoval {
    if ('CsrVerifiedRemoval' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class CsrVerifiedRemoval {
    [StructLayout(LayoutKind.Sequential)]
    private struct AttributeTag { public uint Attributes; public uint Tag; }
    [StructLayout(LayoutKind.Sequential)]
    private struct Disposition { public byte DeleteFile; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileInformation {
        public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh,
            WriteLow, WriteHigh, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint mode, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandleEx(SafeFileHandle handle, int kind, out AttributeTag info, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int kind, ref Disposition info, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInformation info);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern uint GetFileType(SafeFileHandle handle);
    private static SafeFileHandle Open(string path, bool directory, bool remove) {
        uint access = directory ? 0x80u : 0x80000000u;
        if (remove) access |= 0x10000u;
        uint flags = directory ? 0x02200000u : 0x00200000u;
        var handle = CreateFileW(path, access, directory ? 3u : 1u, IntPtr.Zero, 3u, flags, IntPtr.Zero);
        if (handle.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        try {
            AttributeTag info;
            if (!GetFileInformationByHandleEx(handle, 9, out info, 8)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if ((info.Attributes & 0x400u) != 0 || ((info.Attributes & 0x10u) != 0) != directory)
                throw new IOException("リンクまたは対象種別の変更を検出しました。");
            return handle;
        } catch { handle.Dispose(); throw; }
    }
    public static SafeFileHandle OpenDirectory(string path, bool remove) { return Open(path, true, remove); }
    public static FileStream OpenFile(string path) {
        var handle = Open(path, false, true);
        try { return new FileStream(handle, FileAccess.Read); }
        catch { handle.Dispose(); throw; }
    }
    public static FileStream OpenGuiLease(string path) {
        // share=0 is FileShare.None; OPEN_REPARSE_POINT refuses link substitution.
        var handle = CreateFileW(path, 0xC0000000u, 0u, IntPtr.Zero, 4u, 0x00200000u, IntPtr.Zero);
        if (handle.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        try {
            FileInformation info;
            if (!GetFileInformationByHandle(handle, out info)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if (GetFileType(handle) != 1u || (info.Attributes & 0x410u) != 0 || info.Links != 1u ||
                info.SizeHigh != 0u || info.SizeLow > 1u)
                throw new IOException("起動ロックのリンクまたは種類を確認できません。");
            return new FileStream(handle, FileAccess.ReadWrite);
        } catch { handle.Dispose(); throw; }
    }
    public static void MarkDelete(SafeFileHandle handle) {
        var info = new Disposition { DeleteFile = 1 };
        if (!SetFileInformationByHandle(handle, 4, ref info, 1)) throw new Win32Exception(Marshal.GetLastWin32Error());
    }
}
'@
}
function Remove-CsrSettings([string]$Root) {
    Assert-CsrSafePath $Root
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not [IO.Path]::GetFileName($rootPath)) { throw '設定保存先が不正です。' }
    $lockPath = $rootPath + '.gui.lock'
    $pins = [Collections.Generic.List[object]]::new()
    $lease = $null
    Initialize-CsrVerifiedRemoval
    try {
        # 祖先の名前を固定し、sibling lockも設定保存先も同じ場所を指し続ける。
        $ancestors = [Collections.Generic.List[string]]::new()
        $cursor = [IO.Path]::GetDirectoryName($rootPath)
        while ($cursor) {
            $ancestors.Insert(0, $cursor)
            $cursor = [IO.Path]::GetDirectoryName($cursor)
        }
        foreach ($directory in $ancestors) {
            $pins.Add([CsrVerifiedRemoval]::OpenDirectory($directory, $false))
        }
        Assert-CsrSafePath $lockPath
        try { $lease = [CsrVerifiedRemoval]::OpenGuiLease($lockPath) }
        catch { throw '起動ロックが使用中または不正のため設定を保持しました。' }
        Assert-CsrSafePath $lockPath
        Assert-CsrSafePath $rootPath
        _Remove-CsrSettingsLocked $rootPath $lease
    } finally {
        if ($null -ne $lease) { $lease.Dispose() }
        for ($index = $pins.Count - 1; $index -ge 0; $index--) { $pins[$index].Dispose() }
    }
}
# 内部専用。公開入口Remove-CsrSettingsがsibling lockを保持して呼び出す。
function _Remove-CsrSettingsLocked([string]$Root, [IO.FileStream]$GuiLease) {
    if ($null -eq $GuiLease -or $GuiLease.SafeFileHandle.IsClosed -or $GuiLease.SafeFileHandle.IsInvalid) {
        throw '設定削除の起動ロックがありません。'
    }
    Assert-CsrSafePath $Root
    if (-not (Test-Path -LiteralPath $Root)) { return }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw '設定保存先が不正です。' }
    $files = [Collections.Generic.List[object]]::new()
    $directories = [Collections.Generic.List[string]]::new()
    $runtimes = [Collections.Generic.List[object]]::new()
    $streams = @{}
    $pins = @{}
    $pinOrder = [Collections.Generic.List[string]]::new()
    Initialize-CsrVerifiedRemoval
    try {
        $rootPath = [IO.Path]::GetFullPath($Root)
        $ancestors = [Collections.Generic.List[string]]::new()
        $cursor = $rootPath
        while ($cursor) {
            $ancestors.Insert(0, $cursor)
            $cursor = [IO.Path]::GetDirectoryName($cursor)
        }
        foreach ($directory in $ancestors) {
            $pins[$directory] = [CsrVerifiedRemoval]::OpenDirectory($directory, $directory -eq $rootPath)
            $pinOrder.Add($directory)
        }
        foreach ($file in Get-ChildItem -LiteralPath $Root -Force) {
            Assert-CsrSafePath $file.FullName
            if ($file.PSIsContainer -and $file.Name -ceq 'runtime') {
                $pins[$file.FullName] = [CsrVerifiedRemoval]::OpenDirectory($file.FullName, $true)
                $pinOrder.Add($file.FullName)
                foreach ($runtime in Get-ChildItem -LiteralPath $file.FullName -Force) {
                    Assert-CsrSafePath $runtime.FullName
                    if (-not $runtime.PSIsContainer -or $runtime.Name -cnotmatch '^[0-9a-f]{64}$') {
                        throw '未知の実行環境を保持して停止しました。'
                    }
                    $pins[$runtime.FullName] = [CsrVerifiedRemoval]::OpenDirectory($runtime.FullName, $true)
                    $pinOrder.Add($runtime.FullName)
                    $children = @(Get-ChildItem -LiteralPath $runtime.FullName -Force)
                    foreach ($child in $children) {
                        Assert-CsrSafePath $child.FullName
                        if ($child.PSIsContainer -or $child.LinkType) { throw '実行環境のリンクまたは未知の配置を保持しました。' }
                        $files.Add($child)
                    }
                    $runtimes.Add(@{Root=$runtime; Files=$children})
                    $directories.Add($runtime.FullName)
                }
                $directories.Add($file.FullName)
            } elseif (-not $file.PSIsContainer -and -not $file.LinkType -and
                      $file.Name -cin @('resume.sqlite3','resume.sqlite3-wal','resume.sqlite3-shm','monitor.lock')) {
                $files.Add($file)
            } else {
                throw '未知の設定ファイルを保持して停止しました。'
            }
        }
        foreach ($file in $files) {
            if (($file.Attributes -band [IO.FileAttributes]::ReadOnly) -ne 0) { throw '読み取り専用の設定または実行環境を保持しました。' }
            $streams[$file.FullName] = [CsrVerifiedRemoval]::OpenFile($file.FullName)
        }
        $required = @('codex.exe','codex-code-mode-host.exe','codex-command-runner.exe','codex-windows-sandbox-setup.exe')
        $allowed = $required + @('codex-windows-sandbox-service.exe')
        $markerName = '.codex-safe-resume-runtime.json'
        foreach ($runtime in $runtimes) {
            foreach ($file in $runtime.Files) {
                if ($file.Name -cnotin ($allowed + @($markerName))) { throw '未知の実行環境ファイルを保持しました。' }
            }
            $markerPath = Join-Path $runtime.Root.FullName $markerName
            if (-not $streams.ContainsKey($markerPath)) { throw '実行環境の所有記録がありません。' }
            $reader = [IO.StreamReader]::new($streams[$markerPath], [Text.Encoding]::UTF8, $true, 1024, $true)
            try { $marker = $reader.ReadToEnd() | ConvertFrom-Json -AsHashtable }
            finally { $reader.Dispose() }
            if ($marker -isnot [Collections.IDictionary] -or $marker.Count -ne 3 -or
                $marker.schema -cne 'codex-safe-resume.runtime/v1' -or
                $marker.key -cne $runtime.Root.Name -or $marker.files -isnot [Collections.IDictionary]) {
                throw '実行環境の所有記録が不正です。'
            }
            foreach ($name in $required) {
                if (-not $marker.files.Contains($name)) { throw '実行環境の必須ファイルがありません。' }
            }
            if ($runtime.Files.Count -ne $marker.files.Count + 1) { throw '未知の実行環境ファイルを保持しました。' }
            foreach ($name in $marker.files.Keys) {
                if ($name -cnotin $allowed -or $marker.files[$name] -isnot [string] -or
                    $marker.files[$name] -cnotmatch '^[0-9a-f]{64}$') { throw '実行環境のファイル記録が不正です。' }
                $path = Join-Path $runtime.Root.FullName $name
                if (-not $streams.ContainsKey($path)) { throw '実行環境の記録と配置が一致しません。' }
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $hash = [Convert]::ToHexString($sha.ComputeHash($streams[$path])).ToLowerInvariant() }
                finally { $sha.Dispose() }
                if ($hash -cne $marker.files[$name]) { throw '実行環境の変更を保持して停止しました。' }
            }
        }
        foreach ($file in $files) { [CsrVerifiedRemoval]::MarkDelete($streams[$file.FullName].SafeFileHandle) }
        foreach ($stream in $streams.Values) { $stream.Dispose() }
        foreach ($directory in $directories) {
            [CsrVerifiedRemoval]::MarkDelete($pins[$directory])
            $pins[$directory].Dispose()
        }
        [CsrVerifiedRemoval]::MarkDelete($pins[$rootPath])
    } finally {
        foreach ($stream in $streams.Values) { $stream.Dispose() }
        for ($index = $pinOrder.Count - 1; $index -ge 0; $index--) { $pins[$pinOrder[$index]].Dispose() }
    }
}

function Publish-CsrShortcut([string]$Temporary, [string]$Destination) {
    Assert-CsrSafePath $Temporary
    Assert-CsrSafePath $Destination
    # The two-argument move fails if Destination exists, including a racing writer.
    [IO.File]::Move($Temporary, $Destination)
}
