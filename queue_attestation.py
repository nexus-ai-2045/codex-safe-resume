from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import errno
import os
from pathlib import Path
import stat
import sys

from core import reject_links


_ERROR = '確認済みの公式ファイルまたは権限が変更されました'
_LIMIT = 40000


def _windows_security(path):
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = security.GetNamedSecurityInfoW
    query.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    query.restype = wintypes.DWORD
    length = security.GetSecurityDescriptorLength
    length.argtypes = [ctypes.c_void_p]
    length.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if query(str(path), 1, 7, None, None, None, None, ctypes.byref(descriptor)) != 0:
        raise ValueError(_ERROR)
    try:
        size = length(descriptor)
        if not 0 < size <= 1048576:
            raise ValueError(_ERROR)
        return ctypes.string_at(descriptor, size)
    finally:
        if kernel.LocalFree(descriptor):
            raise ValueError(_ERROR)


def _darwin_security(path):
    library = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    library.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.acl_get_link_np.restype = ctypes.c_void_p
    library.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    library.acl_to_text.restype = ctypes.c_void_p
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    ctypes.set_errno(0)
    acl = library.acl_get_link_np(os.fsencode(path), 0x100)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            path.lstat()
            return b'no-extended-acl'
        raise ValueError(_ERROR)
    text = None
    try:
        size = ctypes.c_ssize_t()
        text = library.acl_to_text(acl, ctypes.byref(size))
        if not text or not 0 <= size.value <= 1048576:
            raise ValueError(_ERROR)
        return b'extended-acl:' + ctypes.string_at(text, size.value)
    finally:
        if text and library.acl_free(text) != 0:
            raise ValueError(_ERROR)
        if library.acl_free(acl) != 0:
            raise ValueError(_ERROR)


def _security(path):
    if sys.platform == 'win32':
        return _windows_security(path)
    if sys.platform == 'darwin':
        return _darwin_security(path)
    raise ValueError(_ERROR)


def _metadata(info):
    mode = info.st_mode & ~0o111 if os.name == 'nt' else info.st_mode
    links = None if stat.S_ISDIR(info.st_mode) else info.st_nlink
    return (info.st_dev, info.st_ino, mode, links,
            info.st_uid, info.st_gid, getattr(info, 'st_file_attributes', 0))


def _snapshot(path, *, sealed_root=None):
    info = path.lstat()
    base = _metadata(info)
    if getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError(_ERROR)
    if stat.S_ISLNK(info.st_mode):
        if sealed_root is None or not path.resolve(strict=True).is_relative_to(sealed_root):
            raise ValueError(_ERROR)
        content = ('link', os.readlink(path))
    elif stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise ValueError(_ERROR)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if _metadata(opened) != base:
                raise ValueError(_ERROR)
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if (_metadata(after) != base or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns):
            raise ValueError(_ERROR)
        content = ('file', info.st_size, digest.hexdigest())
    elif stat.S_ISDIR(info.st_mode):
        content = ('directory',)
    else:
        raise ValueError(_ERROR)
    permissions = _security(path)
    if _metadata(path.lstat()) != base:
        raise ValueError(_ERROR)
    return base, content, permissions


def _bundle_entries(root):
    entries = {root}
    pending = [root]
    while pending:
        current = pending.pop()
        for path in current.iterdir():
            entries.add(path)
            if len(entries) > _LIMIT:
                raise ValueError(_ERROR)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
    return entries


def capture(files, *, tree=None, exact_directories=(), package_boundary=None):
    files = {Path(path).absolute() for path in files}
    for path in files:
        reject_links(path)
    root = Path(tree).absolute() if tree is not None else None
    boundary = Path(package_boundary).absolute() if package_boundary is not None else None
    if boundary is not None:
        reject_links(boundary)
        if not boundary.is_dir():
            raise ValueError(_ERROR)
    if root is not None:
        reject_links(root)
        files.update(_bundle_entries(root))
    paths = set(files)
    for path in files:
        for parent in path.parents:
            paths.add(parent)
            if boundary is not None and path.is_relative_to(boundary) and parent == boundary:
                break
    if len(paths) > _LIMIT:
        raise ValueError(_ERROR)
    ordered = sorted(paths, key=str)
    values = {str(path): _snapshot(path, sealed_root=root) for path in ordered}
    directory_entries = {str(Path(path)): tuple(sorted(child.name for child in Path(path).iterdir()))
                         for path in exact_directories}
    return dict(paths=values, tree=str(root) if root is not None else None,
                directories=directory_entries)


def matches(evidence):
    if not isinstance(evidence, dict) or not evidence.get('paths'):
        return False
    root = Path(evidence['tree']) if evidence.get('tree') is not None else None
    if root is not None:
        known = {Path(path) for path in evidence['paths'] if Path(path).is_relative_to(root)}
        if _bundle_entries(root) != known:
            return False
    for path, value in evidence.get('directories', {}).items():
        if tuple(sorted(child.name for child in Path(path).iterdir())) != value:
            return False
    for name, expected in evidence['paths'].items():
        path = Path(name)
        if root is None or not path.is_relative_to(root) or not stat.S_ISLNK(path.lstat().st_mode):
            reject_links(path)
        if _snapshot(path, sealed_root=root) != expected:
            return False
    return True
