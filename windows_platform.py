from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes as wt
from functools import lru_cache
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import cli_security
from core import reject_links


_ERROR = 'Windowsの実体・所有者・権限を確認できません'
_AF_UNIX_TAG = 0x80000023


class _ProcessExited(ValueError):
    pass


def _unavailable_process(handle):
    if _native()[0].WaitForSingleObject(handle, 100) == 0:
        raise _ProcessExited(_ERROR)
    raise ValueError(_ERROR)


_ENDPOINT_ACL_SCRIPT = cli_security._ACL_SCRIPT.replace(
    'while ($null -ne $item) {', '$first = $true\n    while ($null -ne $item) {').replace(
    'if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {',
    'if ((($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -and -not $first) {').replace(
    'if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }',
    '$first = $false\n        if ($item -is [System.IO.FileInfo]) { $item = $item.Directory }')


class _ProcessBasic(ctypes.Structure):
    _fields_ = [('reserved1', ctypes.c_void_p), ('peb', ctypes.c_void_p),
                ('reserved2', ctypes.c_void_p * 2), ('pid', ctypes.c_size_t),
                ('ppid', ctypes.c_size_t)]


class _FileInfo(ctypes.Structure):
    _fields_ = [('attributes', wt.DWORD), ('creation', wt.FILETIME),
                ('access', wt.FILETIME), ('write', wt.FILETIME),
                ('volume', wt.DWORD), ('size_high', wt.DWORD), ('size_low', wt.DWORD),
                ('links', wt.DWORD), ('index_high', wt.DWORD), ('index_low', wt.DWORD)]


class _TagInfo(ctypes.Structure):
    _fields_ = [('attributes', wt.DWORD), ('tag', wt.DWORD)]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [('size', wt.DWORD), ('usage', wt.DWORD), ('pid', wt.DWORD),
                ('heap', ctypes.c_size_t), ('module', wt.DWORD), ('threads', wt.DWORD),
                ('ppid', wt.DWORD), ('priority', wt.LONG), ('flags', wt.DWORD),
                ('executable', wt.WCHAR * 260)]


def _require_windows():
    if sys.platform != 'win32':
        raise ValueError(_ERROR)


@lru_cache(maxsize=1)
def _native():
    _require_windows()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    nt = ctypes.WinDLL('ntdll', use_last_error=True)
    signatures = (
        (kernel.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
        (kernel.CloseHandle, [wt.HANDLE], wt.BOOL),
        (kernel.GetCurrentProcess, [], wt.HANDLE),
        (kernel.GetProcessTimes, [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4, wt.BOOL),
        (kernel.QueryFullProcessImageNameW,
         [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (kernel.WaitForSingleObject, [wt.HANDLE, wt.DWORD], wt.DWORD),
        (security.OpenProcessToken, [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)], wt.BOOL),
        (security.GetTokenInformation,
         [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD)], wt.BOOL),
        (security.EqualSid, [ctypes.c_void_p, ctypes.c_void_p], wt.BOOL),
        (nt.NtQueryInformationProcess,
         [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG)], wt.LONG),
        (kernel.CreateFileW,
         [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE], wt.HANDLE),
        (kernel.GetFileInformationByHandle, [wt.HANDLE, ctypes.POINTER(_FileInfo)], wt.BOOL),
        (kernel.GetFileInformationByHandleEx,
         [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL),
        (kernel.CreateToolhelp32Snapshot, [wt.DWORD, wt.DWORD], wt.HANDLE),
        (kernel.Process32FirstW, [wt.HANDLE, ctypes.POINTER(_ProcessEntry)], wt.BOOL),
        (kernel.Process32NextW, [wt.HANDLE, ctypes.POINTER(_ProcessEntry)], wt.BOOL),
    )
    for function, args, result in signatures:
        function.argtypes, function.restype = args, result
    return kernel, security, nt


@contextmanager
def _handle(value):
    if value in (None, 0, ctypes.c_void_p(-1).value):
        raise ValueError(_ERROR)
    try:
        yield value
    finally:
        _native()[0].CloseHandle(value)


def _token_user(process):
    security = _native()[1]
    token = wt.HANDLE()
    if not security.OpenProcessToken(process, 8, ctypes.byref(token)):
        raise ValueError(_ERROR)
    with _handle(token.value):
        size = wt.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value < 65536:
            raise ValueError(_ERROR)
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(size)):
            raise ValueError(_ERROR)
        return buffer


@contextmanager
def _process(pid):
    _require_windows()
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError(_ERROR)
    kernel, security, nt = _native()
    value = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not value and ctypes.get_last_error() == 87:
        raise _ProcessExited(_ERROR)
    with _handle(value) as handle:
        try:
            owner = _token_user(handle)
        except ValueError:
            _unavailable_process(handle)
        current = _token_user(kernel.GetCurrentProcess())
        if not security.EqualSid(ctypes.cast(owner, ctypes.POINTER(ctypes.c_void_p))[0],
                                 ctypes.cast(current, ctypes.POINTER(ctypes.c_void_p))[0]):
            raise ValueError(_ERROR)
        times = [wt.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            _unavailable_process(handle)
        name = ctypes.create_unicode_buffer(32768)
        length = wt.DWORD(len(name))
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
            _unavailable_process(handle)
        executable = Path(name.value)
        if not executable.is_absolute():
            raise ValueError(_ERROR)
        info = _ProcessBasic()
        returned = wt.ULONG()
        if nt.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info),
                                        ctypes.byref(returned)) != 0:
            _unavailable_process(handle)
        if returned.value != ctypes.sizeof(info) or info.pid != pid:
            raise ValueError(_ERROR)
        if kernel.WaitForSingleObject(handle, 0) != 258:
            _unavailable_process(handle)
        yield handle, dict(pid=pid, ppid=info.ppid,
                           start=[times[0].dwLowDateTime, times[0].dwHighDateTime],
                           executable=str(executable.resolve(strict=True)))


def process_identity(pid):
    try:
        with _process(pid) as (_, identity):
            return identity
    except _ProcessExited:
        raise
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError):
        raise ValueError(_ERROR) from None


def command_line_arguments(command):
    _require_windows()
    if not isinstance(command, str) or not command or len(command) > 32767 or '\x00' in command:
        raise ValueError(_ERROR)
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = _native()[0]
    parse = shell.CommandLineToArgvW
    parse.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wt.LPWSTR)
    free = kernel.LocalFree
    free.argtypes, free.restype = [ctypes.c_void_p], ctypes.c_void_p
    count = ctypes.c_int()
    values = parse(command, ctypes.byref(count))
    if not values:
        raise ValueError(_ERROR)
    try:
        if not 0 < count.value <= 256:
            raise ValueError(_ERROR)
        return [values[index] for index in range(count.value)]
    finally:
        free(ctypes.cast(values, ctypes.c_void_p))


def desktop_bundle(pid):
    return process_identity(pid)['executable']


def _endpoint_acl(path):
    result = subprocess.run(
        [str(cli_security._system_powershell()), '-NoProfile', '-NonInteractive',
         '-Command', _ENDPOINT_ACL_SCRIPT], shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=cli_security._ACL_TIMEOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        env=dict(os.environ, CSR_CLI_TO_VERIFY=str(path)))
    if result.returncode != 0 or result.stdout != b'true':
        raise ValueError(_ERROR)


def _file_info(handle):
    info, tag = _FileInfo(), _TagInfo()
    kernel = _native()[0]
    if (not kernel.GetFileInformationByHandle(handle, ctypes.byref(info))
            or not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag))):
        raise ValueError(_ERROR)
    return info, tag


def _file_identity(info):
    return dict(device=info.volume, inode=(info.index_high << 32) | info.index_low,
                epoch=(info.creation.dwHighDateTime << 32) | info.creation.dwLowDateTime)


def open_gui_lock(path):
    """GUI制御ファイルを開き、元のWindows取得エラーをCRT変換前に保持する。"""
    _require_windows()
    import msvcrt
    reject_links(path)
    kernel = _native()[0]
    handle = kernel.CreateFileW(str(path), 0xC0000000, 3, None, 4, 0x00200000, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        # 他のnative APIを呼ぶ前に取得する。errno=13だけからbusyを推測しない。
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    owned = True
    try:
        info, tag = _file_info(handle)
        if ((info.attributes | tag.attributes) & 0x410 or info.links != 1
                or info.size_high != 0 or info.size_low > 1):
            raise RuntimeError('起動ロックのリンクまたは種類を確認できません')
        flags = os.O_RDWR | os.O_BINARY | os.O_NOINHERIT
        fd = msvcrt.open_osfhandle(handle, flags)
        owned = False  # 以降のCloseHandleは返却fdのos.closeだけが担当する。
        return fd
    except (OSError, ValueError) as error:
        # 検査・移譲の失敗を、CreateFileWの共有違反と混同しない。
        raise RuntimeError('起動ロックの検査またはハンドル移譲に失敗しました') from error
    finally:
        if owned:
            kernel.CloseHandle(handle)


def endpoint_identity(path):
    try:
        _require_windows()
        path = Path(path)
        if (not path.is_absolute() or path.name in ('', '.', '..')
                or '..' in path.parts or ':' in path.name):
            raise ValueError(_ERROR)
        reject_links(path.parent)
        kernel = _native()[0]
        with ExitStack() as stack:
            parts = tuple(reversed((path, *path.parents)))
            for index, part in enumerate(parts):
                handle = stack.enter_context(_handle(kernel.CreateFileW(
                    str(part), 0x80, 3, None, 3, 0x02000000 | 0x00200000, None)))
                info, tag = _file_info(handle)
                if index == len(parts) - 1:
                    if (not tag.attributes & 0x400 or tag.attributes & 0x10
                            or tag.tag != _AF_UNIX_TAG or info.links != 1):
                        raise ValueError(_ERROR)
                    identity, leaf = _file_identity(info), handle
                elif not tag.attributes & 0x10 or tag.attributes & 0x400:
                    raise ValueError(_ERROR)
            _endpoint_acl(path)
            reject_links(path.parent)
            final, tag = _file_info(leaf)
            if _file_identity(final) != identity or tag.tag != _AF_UNIX_TAG:
                raise ValueError(_ERROR)
            return identity
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError):
        raise ValueError(_ERROR) from None


def _matching_processes(executable):
    kernel = _native()[0]
    result = []
    with _handle(kernel.CreateToolhelp32Snapshot(2, 0)) as snapshot:
        entry = _ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not available:
            raise ValueError(_ERROR)
        while available:
            if entry.executable.casefold() == Path(executable).name.casefold():
                try:
                    identity = process_identity(entry.pid)
                except _ProcessExited:
                    pass
                else:
                    if os.path.normcase(identity['executable']) == os.path.normcase(executable):
                        result.append(identity)
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:
            raise ValueError(_ERROR)
    return result


def _stable_processes(executable):
    for attempt in range(3):
        try:
            return _matching_processes(executable)
        except ValueError:
            if attempt == 2:
                raise
            time.sleep(.01)


def _wait_for_authority(executable, authority, identities, timeout, on_ready):
    if (not isinstance(authority, dict) or set(authority) != {'pid', 'ppid', 'start', 'executable'}
            or type(authority['pid']) is not int or not 0 < authority['pid'] <= 0xffffffff
            or type(authority['ppid']) is not int or not 0 <= authority['ppid'] <= 0xffffffff
            or not isinstance(authority['start'], list) or len(authority['start']) != 2
            or any(type(value) is not int or not 0 <= value <= 0xffffffff for value in authority['start'])
            or not isinstance(authority['executable'], str)
            or os.path.normcase(authority['executable']) != os.path.normcase(executable)):
        raise ValueError(_ERROR)
    authority = dict(authority, start=list(authority['start']))
    kernel = _native()[0]
    with ExitStack() as stack:
        expected, handles = {}, {}

        def admit(current):
            if len({item['pid'] for item in current}) != len(current):
                raise ValueError(_ERROR)
            pending = {item['pid']: item for item in current}
            while pending:
                progress = False
                for pid, identity in list(pending.items()):
                    if pid in expected:
                        if expected[pid] != identity:
                            raise ValueError(_ERROR)
                    else:
                        if pid == authority['pid']:
                            if identity != authority:
                                raise ValueError(_ERROR)
                        else:
                            parent = expected.get(identity['ppid'])
                            if parent is None:
                                continue
                            if (identity['start'][1], identity['start'][0]) < (parent['start'][1], parent['start'][0]):
                                raise ValueError(_ERROR)
                        try:
                            handle, actual = stack.enter_context(_process(pid))
                        except _ProcessExited:
                            del pending[pid]
                            progress = True
                            continue
                        if actual != identity or os.path.normcase(actual['executable']) != os.path.normcase(executable):
                            raise ValueError(_ERROR)
                        expected[pid], handles[pid] = actual, handle
                    del pending[pid]
                    progress = True
                if not progress:
                    raise ValueError(_ERROR)

        admit(identities)
        if authority['pid'] not in handles:
            raise ValueError(_ERROR)
        deadline, notified = time.monotonic() + timeout, False
        while True:
            current = _stable_processes(executable)
            admit(current)
            states = {pid: kernel.WaitForSingleObject(handle, 0) for pid, handle in handles.items()}
            if any(value not in (0, 258) for value in states.values()):
                raise ValueError(_ERROR)
            if all(value == 0 for value in states.values()):
                if _stable_processes(executable) or _stable_processes(executable):
                    raise ValueError(_ERROR)
                return
            if not notified and on_ready is not None and states[authority['pid']] == 258:
                try:
                    on_ready()
                except Exception:
                    raise ValueError(_ERROR) from None
                notified = True
            if time.monotonic() >= deadline:
                raise ValueError(_ERROR)
            time.sleep(.1)


def quit_desktop(identifier, executable, *, timeout=30, on_ready=None, authority=None):
    try:
        _require_windows()
        executable = str(Path(executable).resolve(strict=True))
        if (not isinstance(identifier, str)
                or os.path.normcase(identifier) != os.path.normcase(executable)
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 300
                or (on_ready is not None and not callable(on_ready))):
            raise ValueError(_ERROR)
        identities = _stable_processes(executable)
        if not identities:
            if _stable_processes(executable):
                raise ValueError(_ERROR)
            return
        if authority is not None:
            return _wait_for_authority(executable, authority, identities, timeout, on_ready)
        kernel = _native()[0]
        with ExitStack() as stack:
            handles = {}
            expected = {}
            for identity in identities:
                handle, actual = stack.enter_context(_process(identity['pid']))
                if (actual != identity or identity['pid'] in handles
                        or os.path.normcase(actual['executable']) != os.path.normcase(executable)):
                    raise ValueError(_ERROR)
                handles[identity['pid']] = handle
                expected[identity['pid']] = identity
            deadline = time.monotonic() + timeout
            notified = False
            while True:
                states = [kernel.WaitForSingleObject(handle, 0) for handle in handles.values()]
                if any(state not in (0, 258) for state in states):
                    raise ValueError(_ERROR)
                current = _stable_processes(executable)
                if any(expected.get(identity['pid']) != identity for identity in current):
                    raise ValueError('別のDesktop起動を検出しました')
                if all(state == 0 for state in states):
                    if current:
                        raise ValueError(_ERROR)
                    return
                if on_ready is not None and not notified:
                    if (any(state != 258 for state in states) or len(current) != len(expected)
                            or {identity['pid']: identity for identity in current} != expected):
                        raise ValueError(_ERROR)
                    live = [kernel.WaitForSingleObject(handle, 0) for handle in handles.values()]
                    if all(state == 0 for state in live):
                        if _stable_processes(executable):
                            raise ValueError(_ERROR)
                        return
                    if any(state != 258 for state in live):
                        raise ValueError(_ERROR)
                    try:
                        on_ready()
                    except Exception:
                        raise ValueError(_ERROR) from None
                    notified = True
                if time.monotonic() >= deadline:
                    raise ValueError('Desktopの正常終了を確認できません')
                time.sleep(.1)
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError):
        raise ValueError(_ERROR) from None
