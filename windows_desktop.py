import json
import os
from pathlib import Path
import subprocess
import sys

from cli_security import validate_location


_ERROR = 'Windows Desktopと公式CLIの配置を確認できません'
_VERIFY_STARTUP_CLI = r'''
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [System.IO.Path]::Combine($PSHOME, 'Modules')
try {
    $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $env:CSR_CLI_TO_VERIFY
    if ($signature.Status -ne 'Valid' -or
        $signature.SignerCertificate.GetNameInfo(
            [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -cne
        'OpenAI OpCo, LLC') { throw 'invalid' }
    [Console]::Out.Write('true')
} catch { exit 1 }
'''
_DISCOVER = r'''
$ErrorActionPreference = 'Stop'
$env:PSModulePath = [System.IO.Path]::Combine($PSHOME, 'Modules')
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
    $all = @(CimCmdlets\Get-CimInstance Win32_Process)
    $pairs = @()
    foreach ($child in $all) {
        if ($child.Name -ine 'codex.exe' -or $child.CommandLine -notmatch '\bapp-server\b') { continue }
        $parent = @($all | Where-Object ProcessId -eq $child.ParentProcessId)
        if ($parent.Count -ne 1) { continue }
        $bridgePids = @()
        while ($parent.Count -eq 1 -and $parent[0].Name -ieq 'CodexSafeResumeBridge.exe' -and
               $parent[0].ExecutablePath -eq $env:CSR_EXPECTED_BRIDGE -and $bridgePids.Count -lt 2) {
            $bridgePids += [int]$parent[0].ProcessId
            $parent = @($all | Where-Object ProcessId -eq $parent[0].ParentProcessId)
        }
        if ($parent.Count -ne 1 -or $parent[0].Name -notmatch '^(ChatGPT|Codex)\.exe$') { continue }
        $pairs += [ordered]@{desktop=$parent[0].ExecutablePath; cli=$child.ExecutablePath;
                              desktopPid=[int]$parent[0].ProcessId; cliPid=[int]$child.ProcessId;
                              bridgePids=@($bridgePids); commandLine=$child.CommandLine}
    }
    if ($pairs.Count -ne 1) { throw 'invalid' }
    $pair = $pairs[0]
    foreach ($path in @($pair.desktop, $pair.cli)) {
        $signature = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $path
        if ($signature.Status -ne 'Valid' -or
            $signature.SignerCertificate.GetNameInfo(
                [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne
                'OpenAI OpCo, LLC') { throw 'invalid' }
    }
    [Console]::Out.Write(($pair | ConvertTo-Json -Compress))
} catch { exit 1 }
'''


def resolve_desktop(bridge=None, home=None, *, include_identity=False, include_arguments=False):
    from cli_security import _system_powershell
    from windows_platform import process_identity
    try:
        result = subprocess.run(
            [str(_system_powershell()), '-NoProfile', '-NonInteractive', '-Command', _DISCOVER],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, timeout=15,
            env=dict(os.environ, CSR_EXPECTED_BRIDGE=str(bridge) if bridge is not None else ''),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0 or len(result.stdout) > 32768:
            raise ValueError(_ERROR)
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or
                any(type(value.get(key)) is not int or value[key] <= 0
                    for key in ('desktopPid', 'cliPid'))):
            raise ValueError(_ERROR)
        desktop = Path(value['desktop'])
        original_cli = Path(value['cli'])
        parent = process_identity(value['desktopPid'])
        child = process_identity(value['cliPid'])
        previous = child
        bridge_pids = value.get('bridgePids', [])
        if (not isinstance(bridge_pids, list) or len(bridge_pids) > 2 or
                any(type(pid) is not int or pid <= 0 for pid in bridge_pids) or
                len(set(bridge_pids)) != len(bridge_pids)):
            raise ValueError(_ERROR)
        if bridge_pids:
            if bridge is None:
                raise ValueError(_ERROR)
            pinned_bridge = validate_location(bridge)
            for pid in bridge_pids:
                middle = process_identity(pid)
                if (Path(middle['executable']) != pinned_bridge or previous['ppid'] != middle['pid'] or
                        (previous['start'][1], previous['start'][0]) <
                        (middle['start'][1], middle['start'][0])):
                    raise ValueError(_ERROR)
                previous = middle
        if (Path(parent['executable']) != desktop or Path(child['executable']) != original_cli or
                previous['ppid'] != parent['pid'] or
                (previous['start'][1], previous['start'][0]) < (parent['start'][1], parent['start'][0])):
            raise ValueError(_ERROR)
        package = None
        try:
            desktop = validate_location(desktop)
        except ValueError:
            from windows_package import verify_package
            package = verify_package(parent['pid'], desktop)
        try:
            cli = validate_location(original_cli)
        except ValueError:
            from windows_package import verify_package
            from windows_runtime import stage_cli
            if package is None:
                package = verify_package(parent['pid'], desktop)
            cli = validate_location(stage_cli(package['root'], original_cli, home))
        result = cli, desktop, str(desktop)
        if include_arguments:
            from windows_platform import command_line_arguments
            arguments = command_line_arguments(value.get('commandLine'))
            if (not include_identity or not arguments or
                    os.path.normcase(arguments[0]) != os.path.normcase(str(original_cli))):
                raise ValueError(_ERROR)
            return (*result, dict(parent, start=list(parent['start'])), arguments[1:])
        if include_identity:
            return (*result, dict(parent, start=list(parent['start'])))
        return result
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise ValueError(_ERROR) from None


def _startup_identity(pid):
    from windows_platform import process_identity
    value = process_identity(pid)
    if (not isinstance(value, dict) or type(value.get('pid')) is not int or value['pid'] != pid or
            type(value.get('ppid')) is not int or not 0 < value['ppid'] <= 0xffffffff or
            not isinstance(value.get('start'), (list, tuple)) or len(value['start']) != 2 or
            any(type(part) is not int or not 0 <= part <= 0xffffffff for part in value['start']) or
            not any(value['start']) or not isinstance(value.get('executable'), str) or
            not Path(value['executable']).is_absolute()):
        raise ValueError(_ERROR)
    return dict(value, start=list(value['start']))


def resolve_startup_caller(bridge=None, home=None, *, stage=True):
    from cli_security import _system_powershell
    from core import reject_links
    from windows_package import verify_package
    try:
        pinned_bridge = validate_location(Path(bridge) if bridge is not None else Path(sys.executable))
        child = _startup_identity(os.getpid())
        if Path(child['executable']) != pinned_bridge:
            raise ValueError(_ERROR)
        chain = [child]
        parent = _startup_identity(child['ppid'])
        if Path(parent['executable']) == pinned_bridge:
            chain.append(parent)
            parent = _startup_identity(parent['ppid'])
        if (parent['pid'] in {value['pid'] for value in chain} or
                Path(parent['executable']) == pinned_bridge):
            raise ValueError(_ERROR)
        chain.append(parent)
        for younger, older in zip(chain, chain[1:]):
            if ((younger['start'][1], younger['start'][0]) <
                    (older['start'][1], older['start'][0])):
                raise ValueError(_ERROR)
        desktop = Path(parent['executable'])
        try:
            package = verify_package(parent['pid'], desktop)
        except ValueError:
            from windows_entry import installation
            if installation() != desktop:
                raise ValueError(_ERROR) from None
            package = dict(root=str(desktop.parent.parent))
        root = Path(package['root'])
        if not root.is_absolute() or desktop != root/'app'/'ChatGPT.exe':
            raise ValueError(_ERROR)
        original_cli = root/'app'/'resources'/'codex.exe'
        reject_links(original_cli)
        signature = subprocess.run(
            [str(_system_powershell()), '-NoProfile', '-NonInteractive', '-Command', _VERIFY_STARTUP_CLI],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, timeout=15, env=dict(os.environ, CSR_CLI_TO_VERIFY=str(original_cli)),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if signature.returncode != 0 or signature.stdout != b'true':
            raise ValueError(_ERROR)
        cli = original_cli
        if stage:
            try:
                cli = validate_location(original_cli)
            except ValueError:
                from windows_runtime import stage_cli
                cli = validate_location(stage_cli(package['root'], original_cli, home))
        for expected in chain:
            if _startup_identity(expected['pid']) != expected:
                raise ValueError(_ERROR)
        reject_links(original_cli)
        return cli, desktop, str(desktop), dict(parent, start=list(parent['start']))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        raise ValueError(_ERROR) from None
