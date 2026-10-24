import hashlib
import json
import os
from pathlib import Path
import secrets

from cli_security import validate_location
from core import reject_links, safe_state_dir


REQUIRED_FILES = ('codex.exe', 'codex-code-mode-host.exe', 'codex-command-runner.exe',
                  'codex-windows-sandbox-setup.exe')
OPTIONAL_FILES = ('codex-windows-sandbox-service.exe',)
MARKER = '.codex-safe-resume-runtime.json'
_ERROR = '公式CLIの保護された配置を確認できません'


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(directory, expected):
    reject_links(directory)
    if set(path.name for path in directory.iterdir()) != set(expected) | {MARKER}:
        raise ValueError(_ERROR)
    marker = directory/MARKER
    reject_links(marker)
    if marker.stat().st_size > 16384:
        raise ValueError(_ERROR)
    recorded = json.loads(marker.read_text(encoding='utf-8'))
    if recorded != dict(schema='codex-safe-resume.runtime/v1', key=_cache_key(expected), files=expected):
        raise ValueError(_ERROR)
    for name, digest in expected.items():
        path = validate_location(directory/name)
        if _digest(path) != digest:
            raise ValueError(_ERROR)
    return directory/'codex.exe'


def _cache_key(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def stage_cli(package_root, actual_cli, home=None):
    try:
        resources = Path(package_root)/'app'/'resources'
        actual_cli = Path(actual_cli)
        reject_links(resources)
        reject_links(actual_cli)
        if actual_cli.name.casefold() != 'codex.exe':
            raise ValueError(_ERROR)
        expected = {}
        for name in REQUIRED_FILES:
            source = resources/name
            cached = actual_cli.parent/name
            digest = _digest(source)
            if _digest(cached) != digest:
                raise ValueError(_ERROR)
            expected[name] = digest
        for name in OPTIONAL_FILES:
            if (resources/name).is_file():
                expected[name] = _digest(resources/name)
        key = _cache_key(expected)
        root = safe_state_dir(home)/'runtime'
        reject_links(root)
        root.mkdir(mode=0o700, exist_ok=True)
        destination = root/key
        if destination.exists():
            return _verify(destination, expected)
        staging = root/(key+'.stage.'+secrets.token_hex(16))
        staging.mkdir(mode=0o700)
        for name in expected:
            with (resources/name).open('rb') as source, (staging/name).open('xb') as target:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        with (staging/MARKER).open('x', encoding='utf-8') as marker:
            json.dump(dict(schema='codex-safe-resume.runtime/v1', key=key, files=expected), marker,
                      sort_keys=True)
            marker.flush()
            os.fsync(marker.fileno())
        _verify(staging, expected)
        staging.rename(destination)
        return _verify(destination, expected)
    except (OSError, RuntimeError, ValueError, TypeError):
        raise ValueError(_ERROR) from None
