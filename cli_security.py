"""CLIの実行前に、ファイルと全祖先を他ユーザーが置換できないか照合する。"""
import ctypes
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from core import reject_links


_ERROR = 'CLIの配置先の所有者・権限を確認できません'
_ACL_TIMEOUT = 8

# Get-AclとFileSystemRightsのOS実装を使い、DACLの独自解釈はしない。
# Allowの危険権限はDenyとの相殺を推測せず拒否するため、保守的に停止する。
_ACL_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
# user指定のmodule探索先から同名cmdletを読み込まない。
$env:PSModulePath = [System.IO.Path]::Combine($PSHOME, 'Modules')
try {
    $sidType = [System.Security.Principal.SecurityIdentifier]
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $installer = [System.Security.Principal.NTAccount]::new('NT SERVICE\TrustedInstaller')
    $allowed = @($me, 'S-1-5-18', 'S-1-5-32-544', $installer.Translate($sidType).Value)
    $item = Microsoft.PowerShell.Management\Get-Item -LiteralPath $env:CSR_CLI_TO_VERIFY -Force
    if ($item -isnot [System.IO.FileInfo]) { throw 'invalid' }
    $rightsType = [System.Security.AccessControl.FileSystemRights]
    $common = [int64]$rightsType::WriteData -bor [int64]$rightsType::WriteAttributes -bor
        [int64]$rightsType::WriteExtendedAttributes -bor [int64]$rightsType::Delete -bor
        [int64]$rightsType::ChangePermissions -bor [int64]$rightsType::TakeOwnership
    # GenericWrite/GenericAllも拒否し、未知のgeneric bitが安全側に落ちないようにする。
    $common = $common -bor 0x40000000 -bor 0x10000000
    while ($null -ne $item) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'invalid'
        }
        $acl = Microsoft.PowerShell.Security\Get-Acl -LiteralPath $item.FullName
        if ($allowed -notcontains $acl.GetOwner($sidType).Value) { throw 'invalid' }
        $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new(
            $acl.GetSecurityDescriptorBinaryForm(), 0)
        if ($null -eq $descriptor.DiscretionaryAcl) { throw 'invalid' }
        $mask = $common
        if ($item -is [System.IO.DirectoryInfo]) {
            $mask = $mask -bor [int64]$rightsType::DeleteSubdirectoriesAndFiles
            # volume rootのAddSubdirectory単独は既存の保護された子を置換しない。
            if ($null -ne $item.Parent) { $mask = $mask -bor [int64]$rightsType::CreateDirectories }
        } else {
            $mask = $mask -bor [int64]$rightsType::AppendData
        }
        foreach ($rule in $acl.GetAccessRules($true, $true, $sidType)) {
            if (($rule.PropagationFlags -band
                [System.Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) { continue }
            if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
                continue
            }
            if ($allowed -contains $rule.IdentityReference.Value) { continue }
            # OWNER RIGHTS is the already-validated owner, not another account.
            # Python 3.13 mkdir(0700) uses this SID in its protected DACL.
            if ($rule.IdentityReference.Value -eq 'S-1-3-4') { continue }
            if (([int64]$rule.FileSystemRights -band $mask) -ne 0) { throw 'invalid' }
        }
        if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }
        else { $item = $item.Parent }
    }
    [Console]::Out.Write('true')
} catch {
    [Console]::Out.Write('false')
}
'''


def _system_powershell():
    """PATHや環境変数に頼らず、OSのsystem directoryから固定の実行先を得る。"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    get_directory = kernel32.GetSystemDirectoryW
    get_directory.argtypes = [ctypes.POINTER(ctypes.c_wchar), ctypes.c_uint]
    get_directory.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_directory(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise ValueError(_ERROR)
    system = Path(buffer.value)
    if not system.is_absolute():
        raise ValueError(_ERROR)
    executable = system / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    if not executable.is_file():
        raise ValueError(_ERROR)
    return executable


def _windows_location(path):
    try:
        command = [str(_system_powershell()), '-NoProfile', '-NonInteractive',
                   '-Command', _ACL_SCRIPT]
        result = subprocess.run(
            command, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=_ACL_TIMEOUT,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            env=dict(os.environ, CSR_CLI_TO_VERIFY=str(path)))
        if result.returncode != 0 or result.stdout != b'true':
            raise ValueError(_ERROR)
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        raise ValueError(_ERROR) from None


def _posix_location(path):
    allowed_owners = (0, os.getuid())
    trusted_admin_groups = set()
    if sys.platform == 'darwin':
        import grp
        for name in ('admin', 'wheel'):
            try:
                trusted_admin_groups.add(grp.getgrnam(name).gr_gid)
            except KeyError:
                pass
    child_info = None
    for index, part in enumerate((path, *path.parents)):
        info = part.stat()
        if info.st_uid not in allowed_owners:
            raise ValueError(_ERROR)
        if index == 0:
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not info.st_mode & 0o111 or info.st_mode & 0o022):
                raise ValueError(_ERROR)
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(_ERROR)
            if info.st_mode & 0o022:
                admin_directory = (sys.platform == 'darwin' and info.st_uid == 0
                                   and info.st_gid in trusted_admin_groups
                                   and not info.st_mode & 0o002)
                # sticky保護の例外はroot所有の親と、保護された既存の子だけ。
                protected_child = (child_info.st_uid in allowed_owners
                                   and not child_info.st_mode & 0o022)
                if not (admin_directory or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX
                        and protected_child)):
                    raise ValueError(_ERROR)
        child_info = info


_DARWIN_DANGEROUS = frozenset({
    'write', 'append', 'add_file', 'add_subdirectory', 'delete', 'delete_child',
    'writeattr', 'writeextattr', 'writesecurity', 'chown',
})
_DARWIN_KNOWN = _DARWIN_DANGEROUS | {
    'read', 'execute', 'list', 'search', 'readattr', 'readextattr', 'readsecurity',
    'file_inherit', 'directory_inherit', 'limit_inherit', 'only_inherit', 'inherited',
}
_DARWIN_HEADER = re.compile(r'[-d][rwxstST-]{9}(?P<marker>[+@]?)\s+\S')
_DARWIN_ENTRY = re.compile(
    r'\s*\d+:\s+.+?\s+(?P<kind>allow|deny)\s+'
    r'(?P<rights>[a-z_]+(?:,[a-z_]+)*)\s*')


def _darwin_acl(path):
    """OS付属lsでACLを読む。権限を変更せず、危険なAllowは所有者を問わず止める。"""
    try:
        result = subprocess.run(
            ['/bin/ls', '-lde', '--', str(path)], shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, text=True, encoding='utf-8', errors='strict',
            env=dict(os.environ, LC_ALL='C'))
        if result.returncode != 0 or result.stderr:
            raise ValueError(_ERROR)
        lines = result.stdout.splitlines()
        header = _DARWIN_HEADER.match(lines[0]) if lines else None
        if header is None or (header.group('marker') == '+' and len(lines) < 2):
            raise ValueError(_ERROR)
        for line in lines[1:]:
            entry = _DARWIN_ENTRY.fullmatch(line)
            if entry is None:
                raise ValueError(_ERROR)
            rights = set(entry.group('rights').split(','))
            if rights - _DARWIN_KNOWN:
                raise ValueError(_ERROR)
            if entry.group('kind') == 'allow' and rights & _DARWIN_DANGEROUS:
                raise ValueError(_ERROR)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        raise ValueError(_ERROR) from None


def validate_location(path):
    """確認済みPathを返す。未知の権限、リンク、OSは秘匿したValueErrorで止める。"""
    try:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError(_ERROR)
        reject_links(path)
        if os.name == 'posix':
            # reject_linksで確認したOS標準aliasだけを実体へ揃え、/privateなどの
            # 物理的な祖先も含めて検査する。利用者が作ったリンクは先に拒否済み。
            physical = path.resolve(strict=True)
            _posix_location(physical)
            if sys.platform == 'darwin':
                for part in (physical, *physical.parents):
                    _darwin_acl(part)
        elif os.name == 'nt':
            _windows_location(path)
        else:
            raise ValueError(_ERROR)
        return path
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ValueError(_ERROR) from None
