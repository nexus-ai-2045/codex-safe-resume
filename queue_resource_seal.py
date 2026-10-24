from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import stat

from core import reject_links


_ERROR = '公式bundleのCLI resource sealを確認できません'
_MAX_ENVELOPE = 8 * 1024 * 1024
_MAX_FILES = 40000
_MAX_EXECUTABLE = 2 * 1024 * 1024 * 1024


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns)


def _regular(path, limit):
    reject_links(path)
    info = path.lstat()
    owners = {0, getattr(os, 'getuid', lambda: 0)()}
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid not in owners or not 0 < info.st_size <= limit):
        raise ValueError(_ERROR)
    return _identity(info)


def _read(path, limit):
    before = _regular(path, limit)
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError(_ERROR)
        result = stream.read(limit + 1)
        if len(result) > limit or _identity(os.fstat(stream.fileno())) != before:
            raise ValueError(_ERROR)
    if _regular(path, limit) != before:
        raise ValueError(_ERROR)
    return before, result


def _sha256(path):
    before = _regular(path, _MAX_EXECUTABLE)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError(_ERROR)
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        if _identity(os.fstat(stream.fileno())) != before:
            raise ValueError(_ERROR)
    if _regular(path, _MAX_EXECUTABLE) != before:
        raise ValueError(_ERROR)
    return digest.digest()


class ResourceSeal:
    def __init__(self, contents):
        self.contents = Path(contents)
        if not self.contents.is_absolute() or '..' in self.contents.parts:
            raise ValueError(_ERROR)
        reject_links(self.contents)
        self.path = self.contents / '_CodeSignature' / 'CodeResources'
        self.identity, self.raw = _read(self.path, _MAX_ENVELOPE)
        try:
            envelope = plistlib.loads(self.raw)
        except (ValueError, TypeError, OverflowError):
            raise ValueError(_ERROR) from None
        self.files = envelope.get('files2') if isinstance(envelope, dict) else None
        if not isinstance(self.files, dict) or not 0 < len(self.files) <= _MAX_FILES:
            raise ValueError(_ERROR)

    def resource(self, executable):
        target = Path(executable)
        reject_links(target)
        if (not target.is_absolute() or '..' in target.parts
                or not target.is_relative_to(self.contents)
                or not target.resolve(strict=True).is_relative_to(self.contents.resolve(strict=True))):
            raise ValueError(_ERROR)
        relative = target.relative_to(self.contents).as_posix()
        entry = self.files.get(relative)
        if not isinstance(entry, dict):
            raise ValueError(_ERROR)
        if 'hash2' in entry:
            if (set(entry) - {'hash', 'hash2'} or not isinstance(entry['hash2'], bytes)
                    or len(entry['hash2']) != 32
                    or ('hash' in entry and (not isinstance(entry['hash'], bytes) or len(entry['hash']) != 20))
                    or _sha256(target) != entry['hash2']):
                raise ValueError(_ERROR)
            return True
        if (set(entry) != {'cdhash', 'requirement'}
                or not isinstance(entry['cdhash'], bytes) or len(entry['cdhash']) != 20
                or not isinstance(entry['requirement'], str) or not 0 < len(entry['requirement']) <= 4096):
            raise ValueError(_ERROR)
        return False

    def unchanged(self):
        return _read(self.path, _MAX_ENVELOPE) == (self.identity, self.raw)
