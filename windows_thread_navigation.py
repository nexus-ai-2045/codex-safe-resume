import ctypes
import os
from pathlib import Path
import subprocess
import sys

from core import check_id
import queue_platform
from windows_package import FAMILY, verify_package
from windows_platform import process_identity


_PACKAGE = 'OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0'
_CLASS = 'Chrome_MessageWindow'
_HWND_MESSAGE = -3
_WM_COPYDATA = 0x004A
_FLAGS = 0x0001 | 0x0002 | 0x0020
_TIMEOUT_MS = 5000
_ERROR = 'Windowsの起動中チャット表示先を確認できません'


class _CopyData(ctypes.Structure):
    _fields_ = [('dwData', ctypes.c_size_t), ('cbData', ctypes.c_uint32),
                ('lpData', ctypes.c_void_p)]


def _native():
    if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError(_ERROR)
    library = ctypes.WinDLL('user32', use_last_error=True)
    signatures = (
        ('FindWindowExW', [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p], ctypes.c_void_p),
        ('IsWindow', [ctypes.c_void_p], ctypes.c_int),
        ('GetClassNameW', [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
        ('GetWindowThreadProcessId', [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_uint32),
        ('SendMessageTimeoutW', [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t),
    )
    for name, arguments, result in signatures:
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
    return library


def _handle(value):
    if type(value) is not int or not 0 < value < 2 ** (ctypes.sizeof(ctypes.c_void_p) * 8):
        raise ValueError(_ERROR)
    return value


def _window_pid(library, handle):
    _handle(handle)
    valid = library.IsWindow(handle)
    if type(valid) is not int or valid != 1:
        raise ValueError(_ERROR)
    name = ctypes.create_unicode_buffer(256)
    length = library.GetClassNameW(handle, name, len(name))
    if type(length) is not int or length != len(_CLASS) or name.value != _CLASS:
        raise ValueError(_ERROR)
    pid = ctypes.c_uint32()
    thread = library.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    if type(thread) is not int or not 0 < thread <= 0xffffffff or not pid.value:
        raise ValueError(_ERROR)
    return pid.value


def _unique_window(library, pid):
    found, seen, previous = [], set(), None
    for _ in range(1024):
        handle = library.FindWindowExW(_HWND_MESSAGE, previous, _CLASS, None)
        if handle is None or type(handle) is int and handle == 0:
            if len(found) != 1:
                raise ValueError(_ERROR)
            return found[0]
        _handle(handle)
        if handle in seen:
            raise ValueError(_ERROR)
        seen.add(handle)
        if _window_pid(library, handle) == pid:
            found.append(handle)
            if len(found) > 1:
                raise ValueError(_ERROR)
        previous = handle
    raise ValueError(_ERROR)


def _target(home):
    evidence = queue_platform.desktop_for_home(home)
    desktop = evidence['desktop']
    pid = desktop['pid']
    if (evidence.get('platform') != 'win32' or type(pid) is not int or not 0 < pid <= 0xffffffff
            or not isinstance(desktop.get('executable'), str)
            or Path(evidence['home']) != home or process_identity(pid) != desktop):
        raise ValueError(_ERROR)
    package = verify_package(pid, desktop['executable'])
    root = Path(package['root'])
    executable = Path(desktop['executable'])
    if (package.get('full_name') != _PACKAGE or package.get('family') != FAMILY
            or not root.is_absolute() or '..' in root.parts
            or executable != root / 'app' / 'ChatGPT.exe'
            or package != evidence['attestation']['package']
            or not queue_platform.revalidate(evidence)):
        raise ValueError(_ERROR)
    return evidence, executable


def _payload(executable, uri):
    command = subprocess.list2cmdline([str(executable), '--single-argument', uri])
    value = 'START\0' + str(executable.parent) + '\0' + command + '\0'
    if '\0' in str(executable) or len(value) > 32768:
        raise ValueError(_ERROR)
    return value.encode('utf-16-le')


def _same_window(library, handle, evidence):
    desktop = evidence['desktop']
    return (_unique_window(library, desktop['pid']) == handle
            and _window_pid(library, handle) == desktop['pid']
            and process_identity(desktop['pid']) == desktop)


def _unchanged(library, handle, evidence):
    return queue_platform.revalidate(evidence) and _same_window(library, handle, evidence)


def request_open(uri, *, before_open=None):
    if sys.platform != 'win32' or type(uri) is not str or not uri.startswith('codex://threads/'):
        raise ValueError(_ERROR)
    thread = check_id(uri[len('codex://threads/'):])
    if uri != 'codex://threads/' + thread:
        raise ValueError(_ERROR)
    raw_home = os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))
    if not isinstance(raw_home, str) or not raw_home or '\0' in raw_home:
        raise ValueError(_ERROR)
    home = Path(raw_home).expanduser().absolute()
    evidence, executable = _target(home)
    library = _native()
    handle = _unique_window(library, evidence['desktop']['pid'])
    raw = _payload(executable, uri)
    buffer = ctypes.create_string_buffer(raw, len(raw))
    data = _CopyData(0, len(raw), ctypes.cast(buffer, ctypes.c_void_p))
    if not _unchanged(library, handle, evidence):
        raise ValueError(_ERROR)
    if before_open is not None and before_open() is not True:
        raise ValueError(_ERROR)
    if not _same_window(library, handle, evidence):
        raise ValueError(_ERROR)
    accepted = ctypes.c_size_t()
    result = library.SendMessageTimeoutW(handle, _WM_COPYDATA, 0, ctypes.byref(data),
                                         _FLAGS, _TIMEOUT_MS, ctypes.byref(accepted))
    if (type(result) is not int or result == 0 or accepted.value != 1
            or not _unchanged(library, handle, evidence)):
        raise ValueError(_ERROR)
    return True
