import json
from pathlib import Path
import subprocess

from cli_security import _system_powershell
from core import reject_links


_QUERY = r'''
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [System.IO.Path]::Combine($PSHOME, 'Modules')
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $packages = @(Appx\Get-AppxPackage -Name 'OpenAI.Codex' | Where-Object {
        $_.PackageFamilyName -ceq 'OpenAI.Codex_2p2nqsd0c76g0'
    })
    if ($packages.Count -ne 1) { throw 'invalid' }
    $package = $packages[0]
    [Console]::Out.Write((@{root=$package.InstallLocation;
        full_name=$package.PackageFullName; family=$package.PackageFamilyName} | ConvertTo-Json -Compress))
} catch { exit 1 }
'''


def installation():
    from windows_package import FAMILY, _REQUIRED, _package_record
    result = subprocess.run(
        [str(_system_powershell()), '-NoProfile', '-NonInteractive', '-Command', _QUERY],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=15, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if not 0 < len(result.stdout) <= 32768:
        raise ValueError('公式Codexの登録を確認できません')
    native = json.loads(result.stdout)
    if (not isinstance(native, dict) or native.get('family') != FAMILY
            or not isinstance(native.get('full_name'), str) or not native['full_name']
            or not isinstance(native.get('root'), str)):
        raise ValueError('公式Codexの登録を確認できません')
    root = Path(native['root'])
    reject_links(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('公式Codexの登録を確認できません')
    desktop = root / 'app' / 'ChatGPT.exe'
    reject_links(desktop)
    if not desktop.is_file():
        raise ValueError('公式Codexの実行ファイルを確認できません')
    record = _package_record(native, desktop)
    if (not isinstance(record, dict) or any(record.get(key) is not True for key in _REQUIRED)
            or any(record.get(key) != native[key] for key in ('root', 'family', 'full_name'))):
        raise ValueError('公式Codexの整合性を確認できません')
    return desktop
