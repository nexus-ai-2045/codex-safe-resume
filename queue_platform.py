from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import json
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys

from cli_security import validate_location
from core import check_id, reject_links
from desktop_binding import process_identity as _identity
from windows_platform import _ProcessExited as _WindowsProcessExited


_ERROR = '起動中のCodexと対象チャットの対応を確認できません'
DARWIN_TEAM = '2DC432GLL2'


def _platform():
    return sys.platform


def _regular(path):
    reject_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(_ERROR)
    if os.name == 'posix' and info.st_uid != os.getuid():
        raise ValueError(_ERROR)
    return [info.st_dev, info.st_ino]


def _home(path):
    path = Path(path).expanduser().absolute()
    reject_links(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or (os.name == 'posix' and info.st_uid != os.getuid()):
        raise ValueError(_ERROR)
    return path


def _files(home):
    entries = list(home.iterdir())
    if len(entries) > 4096:
        raise ValueError(_ERROR)
    result = {}
    for name in ('state', 'queue'):
        pattern = re.compile(name + r'_(\d+)\.sqlite\Z')
        choices = [(int(match[1]), path) for path in entries if (match := pattern.fullmatch(path.name))]
        if choices:
            path = max(choices, key=lambda choice: choice[0])[1]
            result[str(path)] = _regular(path)
    if not result:
        raise ValueError(_ERROR)
    return result


class _RmProcess(ctypes.Structure):
    _fields_ = [('pid', wintypes.DWORD), ('created', wintypes.FILETIME)]


class _RmInfo(ctypes.Structure):
    _fields_ = [('identity', _RmProcess), ('application', wintypes.WCHAR * 256),
                ('service', wintypes.WCHAR * 64), ('kind', ctypes.c_int),
                ('status', wintypes.ULONG), ('session', wintypes.DWORD), ('restartable', wintypes.BOOL)]


def _windows_holders(paths):
    library = ctypes.WinDLL('rstrtmgr', use_last_error=True)
    signatures = (
        ('RmStartSession', [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]),
        ('RmRegisterResources', [wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR), wintypes.UINT, ctypes.POINTER(_RmProcess), wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR)]),
        ('RmGetList', [wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_RmInfo), ctypes.POINTER(wintypes.DWORD)]),
        ('RmEndSession', [wintypes.DWORD]),
    )
    for name, arguments in signatures:
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = wintypes.DWORD
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    if library.RmStartSession(ctypes.byref(session), 0, key) != 0:
        raise ValueError(_ERROR)
    try:
        names = (wintypes.LPCWSTR * len(paths))(*(str(path) for path in paths))
        if library.RmRegisterResources(session, len(paths), names, 0, None, 0, None) != 0:
            raise ValueError(_ERROR)
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        rows = None
        for _ in range(4):
            result = library.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), rows, ctypes.byref(reasons))
            if result == 0:
                return [{'pid': row.identity.pid, 'start': [row.identity.created.dwLowDateTime, row.identity.created.dwHighDateTime]}
                        for row in (() if rows is None else rows[:count.value])]
            if result != 234 or not 0 < needed.value <= 1024:
                raise ValueError(_ERROR)
            count.value = needed.value
            rows = (_RmInfo * count.value)()
        raise ValueError(_ERROR)
    finally:
        if library.RmEndSession(session) != 0:
            raise ValueError(_ERROR)


def _holders(paths):
    if _platform() == 'win32':
        return _windows_holders(paths)
    if _platform() != 'darwin':
        raise ValueError(_ERROR)
    result = subprocess.run(['/usr/sbin/lsof', '-t', '-nP', '--', *(str(path) for path in paths)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=5, check=False)
    if result.returncode not in (0, 1) or result.stderr or len(result.stdout) > 65536:
        raise ValueError(_ERROR)
    if result.returncode == 1 and result.stdout:
        raise ValueError(_ERROR)
    values = result.stdout.decode('ascii').splitlines()
    if any(not re.fullmatch(r'[1-9][0-9]{0,9}', value) for value in values):
        raise ValueError(_ERROR)
    identities = [_identity(pid) for pid in sorted({int(value) for value in values})]
    return [{'pid': identity['pid'], 'start': identity['start']} for identity in identities]


def _born(identity):
    value = identity['start']
    if not isinstance(value, (tuple, list)) or len(value) != 2 or any(type(part) is not int or part < 0 for part in value):
        raise ValueError(_ERROR)
    return tuple(reversed(value)) if _platform() == 'win32' else tuple(value)


def _chain(holder):
    current = _identity(holder['pid'])
    if current['start'] != holder['start']:
        raise ValueError(_ERROR)
    windows = _platform() == 'win32'
    names = ('codex.exe', 'codex-code-mode-host.exe') if windows else ('codex', 'codex-code-mode-host')
    chain = [current]
    if Path(current['executable']).name.casefold() == names[1]:
        current = _identity(current['ppid'])
        if Path(current['executable']).name.casefold() != names[0]:
            raise ValueError(_ERROR)
        chain.append(current)
    if Path(current['executable']).name.casefold() != names[0]:
        raise ValueError(_ERROR)
    server = current
    parent = _identity(server['ppid'])
    chain.append(parent)
    if len({item['pid'] for item in chain}) != len(chain):
        raise ValueError(_ERROR)
    if any(_born(child) < _born(older) for child, older in zip(chain, chain[1:])):
        raise ValueError(_ERROR)
    return parent, server, chain


def _windows_cli(desktop, server, chain):
    from cli_security import _system_powershell
    from windows_desktop import _VERIFY_STARTUP_CLI
    from windows_package import verify_package
    from windows_runtime import stage_cli
    try:
        package = verify_package(desktop['pid'], desktop['executable'])
        root = Path(package['root'])
    except ValueError:
        from windows_entry import installation
        installed = installation()
        if Path(desktop['executable']) != installed:
            raise ValueError(_ERROR)
        root = installed.parent.parent
    if Path(desktop['executable']) != root / 'app' / 'ChatGPT.exe':
        raise ValueError(_ERROR)
    original = root / 'app' / 'resources' / 'codex.exe'
    reject_links(original)
    signature = subprocess.run([str(_system_powershell()), '-NoProfile', '-NonInteractive', '-Command', _VERIFY_STARTUP_CLI],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=15, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                               env=dict(os.environ, CSR_CLI_TO_VERIFY=str(original)))
    if signature.returncode != 0 or signature.stdout != b'true':
        raise ValueError(_ERROR)
    for item in chain[:-1]:
        path = Path(item['executable'])
        _regular(path)
        if path.parent != Path(server['executable']).parent:
            raise ValueError(_ERROR)
    return str(validate_location(stage_cli(root, server['executable'])))


def verify_darwin_cli_files(bundle: str | Path, targets: list[Path] | tuple[Path, ...],
                            team: str) -> Path:
    """公式bundle内のCLIだけを検証し、Desktopのプロセス対応は判定しない。"""
    from queue_attestation import capture, matches
    from queue_resource_seal import ResourceSeal, _read
    if team != DARWIN_TEAM:
        raise ValueError(_ERROR)
    bundle = Path(bundle)
    if (not bundle.is_absolute() or '..' in bundle.parts or bundle.suffix != '.app'
            or not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 2):
        raise ValueError(_ERROR)
    reject_links(bundle)
    plist = bundle / 'Contents' / 'Info.plist'
    plist_proof, raw_plist = _read(plist, 1048576)
    info = plistlib.loads(raw_plist)
    name = info.get('CFBundleExecutable') if isinstance(info, dict) else None
    if (not isinstance(info, dict) or info.get('CFBundleIdentifier') != 'com.openai.codex'
            or not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\\' in name):
        raise ValueError(_ERROR)
    contents = bundle / 'Contents'
    executable = contents / 'MacOS' / name
    targets = [Path(path) for path in targets]
    for target in targets:
        reject_links(target)
        if (not target.is_absolute() or '..' in target.parts
                or not target.is_relative_to(contents)
                or target.name not in ('codex', 'codex-code-mode-host')
                or not target.resolve(strict=True).is_relative_to(contents.resolve(strict=True))):
            raise ValueError(_ERROR)
    for target in (executable, *targets):
        validate_location(target)
    seal = ResourceSeal(contents)
    # /var等のOS標準aliasを、既存のattestationが扱う実体パスへ揃える。
    before = capture({path.resolve(strict=True) for path in (executable, plist, seal.path, *targets)})
    requirement = 'anchor apple generic and identifier "com.openai.codex" and certificate leaf[subject.OU] = "' + team + '"'
    for target, constraint in ((bundle, requirement), *((path, 'anchor apple generic and certificate leaf[subject.OU] = "' + team + '"') for path in targets)):
        if target != bundle and seal.resource(target):
            continue
        arguments = ['/usr/bin/codesign', '--verify', '--strict', '-R', '=' + constraint, str(target)]
        if target == bundle:
            arguments.append('--deep')
        result = subprocess.run(arguments,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=30, check=False)
        if result.returncode != 0:
            raise ValueError(_ERROR)
    if (not seal.unchanged() or _read(plist, 1048576) != (plist_proof, raw_plist)
            or not matches(before)):
        raise ValueError(_ERROR)
    for target in (executable, *targets):
        validate_location(target)
    return executable


def _darwin_cli(desktop, server, chain, team):
    from desktop_binding import desktop_bundle
    if team != DARWIN_TEAM:
        raise ValueError(_ERROR)
    executable = Path(desktop['executable'])
    if (not executable.is_absolute() or '..' in executable.parts
            or executable.parent.name != 'MacOS' or executable.parent.parent.name != 'Contents'
            or desktop_bundle(desktop['pid']) != 'com.openai.codex'):
        raise ValueError(_ERROR)
    bundle = executable.parent.parent.parent
    targets = [Path(item['executable']) for item in chain[:-1]]
    if verify_darwin_cli_files(bundle, targets, team) != executable:
        raise ValueError(_ERROR)
    return str(validate_location(Path(server['executable'])))


def _trusted_chain(holder, team):
    desktop, server, chain = _chain(holder)
    before = _attest_source(desktop, server, chain)
    executable = (_windows_cli(desktop, server, chain) if _platform() == 'win32'
                  else _darwin_cli(desktop, server, chain, team))
    if any(_identity(item['pid']) != item for item in chain):
        raise ValueError(_ERROR)
    result = {'desktop': desktop, 'server': server, 'chain': chain, 'executable': executable}
    result['attestation'] = _attest_files(result)
    after = result['attestation']['files']['paths']
    if (before['package'] != result['attestation']['package']
            or any(after.get(path) != proof for path, proof in before['files']['paths'].items())):
        raise ValueError(_ERROR)
    return result


def _windows_package_binding(desktop):
    from windows_package import FAMILY, _native_package
    from windows_platform import _process
    try:
        with _process(desktop['pid']) as (handle, identity):
            if identity != desktop:
                raise ValueError(_ERROR)
            native = _native_package(handle)
    except ValueError:
        from cli_security import _system_powershell
        from windows_entry import _QUERY
        response = subprocess.run([str(_system_powershell()), '-NoProfile', '-NonInteractive', '-Command', _QUERY],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=10, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if response.returncode != 0 or not 0 < len(response.stdout) <= 32768:
            raise ValueError(_ERROR)
        native = json.loads(response.stdout)
    if (not isinstance(native, dict) or native.get('family') != FAMILY
            or not isinstance(native.get('full_name'), str) or not native['full_name']
            or Path(native.get('root', '')) / 'app' / 'ChatGPT.exe' != Path(desktop['executable'])
            or _identity(desktop['pid']) != desktop):
        raise ValueError(_ERROR)
    return native


def _attest_files(result):
    from queue_attestation import capture
    files = {Path(item['executable']) for item in result['chain']}
    executable = Path(result['executable'])
    files.add(executable)
    if _platform() == 'win32':
        from windows_runtime import REQUIRED_FILES, OPTIONAL_FILES, MARKER
        package = _windows_package_binding(result['desktop'])
        root = Path(package['root'])
        folders = {root / 'app' / 'resources', executable.parent,
                   Path(result['server']['executable']).parent}
        for folder in folders:
            for name in REQUIRED_FILES:
                files.add(folder / name)
            for name in OPTIONAL_FILES:
                if (folder / name).exists():
                    files.add(folder / name)
        files.update((executable.parent / MARKER, root / 'AppxManifest.xml'))
        snapshot = capture(files, exact_directories=(executable.parent,), package_boundary=root)
        for folder in (executable.parent, Path(result['server']['executable']).parent):
            for name in REQUIRED_FILES:
                if snapshot['paths'][str(folder / name)][1] != snapshot['paths'][str(root / 'app' / 'resources' / name)][1]:
                    raise ValueError(_ERROR)
        return dict(files=snapshot, package=package)
    if _platform() == 'darwin':
        bundle = Path(result['desktop']['executable']).parent.parent.parent
        return dict(files=capture(files, tree=bundle), package=None)
    raise ValueError(_ERROR)


def _attest_source(desktop, server, chain):
    from queue_attestation import capture
    files = {Path(item['executable']) for item in chain}
    if _platform() == 'win32':
        from windows_runtime import REQUIRED_FILES, OPTIONAL_FILES
        package = _windows_package_binding(desktop)
        root = Path(package['root'])
        for folder in (root / 'app' / 'resources', Path(server['executable']).parent):
            for name in REQUIRED_FILES:
                files.add(folder / name)
            for name in OPTIONAL_FILES:
                if (folder / name).exists():
                    files.add(folder / name)
        files.add(root / 'AppxManifest.xml')
        return dict(files=capture(files, package_boundary=root), package=package)
    if _platform() == 'darwin':
        return dict(files=capture(files, tree=Path(desktop['executable']).parent.parent.parent), package=None)
    raise ValueError(_ERROR)


def _attested_files_match(evidence):
    from queue_attestation import matches
    attestation = evidence['attestation']
    if (_platform() == 'win32'
            and _windows_package_binding(evidence['desktop']) != attestation['package']):
        return False
    if _platform() == 'darwin' and evidence.get('team') != DARWIN_TEAM:
        return False
    return matches(attestation['files'])


def _darwin_arguments(pid):
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    function = libc.sysctl
    function.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    function.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)
    size = ctypes.c_size_t()
    if function(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not 4 < size.value <= 2097152:
        raise ValueError(_ERROR)
    buffer = ctypes.create_string_buffer(size.value)
    if function(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise ValueError(_ERROR)
    raw = buffer.raw[:size.value]
    count = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    if not 0 < count <= 1024:
        raise ValueError(_ERROR)
    start = raw.find(b'\0', 4)
    if start < 0:
        raise ValueError(_ERROR)
    while start < len(raw) and raw[start] == 0:
        start += 1
    parts = raw[start:].split(b'\0')
    if len(parts) <= count:
        raise ValueError(_ERROR)
    return [part.decode('utf-8') for part in parts[:count]]


def _server_role(server):
    if _platform() == 'win32':
        from cli_security import _system_powershell
        from windows_platform import command_line_arguments
        pid = server['pid']
        if type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise ValueError(_ERROR)
        query = "$ErrorActionPreference='Stop'; $env:PSModulePath=[System.IO.Path]::Combine($PSHOME,'Modules'); [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $rows=@(CimCmdlets\\Get-CimInstance Win32_Process -Filter 'ProcessId = " + str(pid) + "'); if($rows.Count -ne 1){exit 1}; [Console]::Out.Write(($rows[0].CommandLine|ConvertTo-Json -Compress))"
        result = subprocess.run([str(_system_powershell()), '-NoProfile', '-NonInteractive', '-Command', query],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=10, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode != 0 or not 0 < len(result.stdout) <= 65536:
            raise ValueError(_ERROR)
        arguments = command_line_arguments(json.loads(result.stdout))
    else:
        arguments = _darwin_arguments(server['pid'])
    if (not arguments or os.path.normcase(arguments[0]) != os.path.normcase(server['executable'])
            or _identity(server['pid']) != server):
        raise ValueError(_ERROR)
    iterator = iter(arguments[1:])
    for argument in iterator:
        if argument in ('-c', '--config', '--enable', '--disable', '--profile'):
            if next(iterator, None) is None:
                raise ValueError(_ERROR)
        elif argument.startswith(('--config=', '--enable=', '--disable=', '--profile=')):
            continue
        elif argument in ('app-server', 'exec-server'):
            return argument
        else:
            raise ValueError(_ERROR)
    raise ValueError(_ERROR)


def _darwin_desktop_parent(desktop):
    from desktop_binding import desktop_bundle
    path = Path(desktop['executable'])
    if path.parent.name != 'MacOS' or path.parent.parent.name != 'Contents':
        return False
    identifier = desktop_bundle(desktop['pid'])
    if (type(identifier) is not str or len(identifier) > 255
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9][A-Za-z0-9-]*)+', identifier) is None
            or _identity(desktop['pid']) != desktop):
        raise ValueError(_ERROR)
    return identifier == 'com.openai.codex'


def _backend_holder(users, known_server=None):
    found = []
    names = {'codex.exe', 'codex-code-mode-host.exe'} if _platform() == 'win32' else {'codex', 'codex-code-mode-host'}
    for holder in users:
        try:
            identity = _identity(holder['pid'])
            if identity['start'] != holder['start']:
                raise ValueError(_ERROR)
            if Path(identity['executable']).name.casefold() in names:
                desktop, server, chain = _chain(holder)
                path = Path(desktop['executable'])
                if _platform() == 'win32':
                    desktop_parent = path.name.casefold() == 'chatgpt.exe'
                else:
                    desktop_parent = _darwin_desktop_parent(desktop)
                if desktop_parent and (server == known_server or _server_role(server) == 'app-server'):
                    found.append(identity)
        except _WindowsProcessExited:
            if _platform() != 'win32':
                raise
    if len(found) != 1:
        raise ValueError(_ERROR)
    return found[0]


def desktop_for_home(codex_home, *, expected_team=None):
    try:
        if _platform() not in ('win32', 'darwin'):
            raise ValueError(_ERROR)
        home = _home(codex_home)
        files = _files(home)
        holder = _backend_holder(_holders(list(files)))
        team = expected_team if expected_team is not None else DARWIN_TEAM
        result = _trusted_chain(holder, team)
        evidence = dict(result, holder=holder, home=str(home), files=files, platform=_platform(), team=team)
        if not revalidate(evidence):
            raise ValueError(_ERROR)
        return evidence
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        raise ValueError(_ERROR) from None


def revalidate(evidence):
    try:
        if not isinstance(evidence, dict) or evidence['platform'] != _platform():
            return False
        home = _home(evidence['home'])
        if _files(home) != evidence['files']:
            return False
        holder = _backend_holder(_holders(list(evidence['files'])), evidence['server'])
        if holder != evidence['holder'] or any(_identity(item['pid']) != item for item in evidence['chain']):
            return False
        desktop, server, chain = _chain(holder)
        if any(value != evidence[key] for key, value in (('desktop', desktop), ('server', server), ('chain', chain))):
            return False
        if not _attested_files_match(evidence):
            return False
        return all(_identity(item['pid']) == item for item in chain)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return False


def loaded(thread_id, evidence):
    try:
        thread_id = check_id(thread_id)
        if not revalidate(evidence):
            return False
        path = _home(evidence['home']) / 'thread-writer-locks' / (thread_id + '.lock')
        file_identity = _regular(path)
        holders = _holders([path])
        if len(holders) != 1:
            return False
        native = _identity(holders[0]['pid'])
        if native['start'] != holders[0]['start']:
            return False
        if native not in (evidence['server'], evidence['holder']):
            desktop, server, linked_chain = _chain(native)
            if (Path(native['executable']).name.casefold() not in ('codex-code-mode-host', 'codex-code-mode-host.exe')
                    or server != evidence['server'] or desktop != evidence['desktop']
                    or str(Path(native['executable'])) not in evidence['attestation']['files']['paths']
                    or any(_identity(item['pid']) != item for item in linked_chain)):
                return False
        return _regular(path) == file_identity and revalidate(evidence)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return False
