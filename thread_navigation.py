"""公式Desktopへ、期限付きのチャットを開く要求だけを渡す。"""
import math
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

from core import check_id, reject_links


class ThreadNavigationError(RuntimeError):
    pass


_MAC_BUNDLE = Path('/Applications/ChatGPT.app')
_NAVIGATION_SECONDS = 3.0
_CLEANUP_SECONDS = .5
_STATUSES = frozenset({'requested', 'unavailable', 'timeout', 'cancelled', 'failed'})
_ERROR = 'チャットを開く要求を確認できません。ChatGPTで対象を開いてください。'


def _mac_target():
    """固定インストール先の軽量な識別検査。送信権限の検査とは独立。"""
    bundle = _MAC_BUNDLE
    plist = bundle / 'Contents/Info.plist'
    reject_links(plist)
    if not bundle.is_dir() or not plist.is_file() or plist.stat().st_size > 1024 * 1024:
        raise ValueError('bundle')
    info = plistlib.loads(plist.read_bytes())
    executable = info.get('CFBundleExecutable')
    if (info.get('CFBundleIdentifier') != 'com.openai.codex'
            or not isinstance(executable, str) or not executable
            or executable in ('.', '..') or '/' in executable or '\\' in executable):
        raise ValueError('identity')
    binary = bundle / 'Contents/MacOS' / executable
    reject_links(binary)
    if not binary.is_file():
        raise ValueError('executable')
    schemes = [scheme for item in info.get('CFBundleURLTypes', [])
               for scheme in item.get('CFBundleURLSchemes', [])]
    if 'codex' not in schemes:
        raise ValueError('uri_support')
    return bundle


def _stop_owned(child, *, group=False):
    """所有する子だけを停止・回収し、後始末の期限を延長しない。"""
    deadline = time.monotonic() + _CLEANUP_SECONDS
    def send(force):
        try:
            if group:
                os.killpg(child.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                child.kill()
            else:
                child.terminate()
        except OSError:
            pass
    send(False)
    try:
        child.wait(timeout=max(0, (deadline - time.monotonic()) / 2))
    except subprocess.TimeoutExpired:
        pass
    # The helper may have exited while its owned opener still holds this group.
    send(True)
    try:
        child.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass


def mac_open_chat(thread_id, deadline):
    """console helper専用。OS受付は表示到着や送信成功を意味しない。"""
    try:
        thread = check_id(thread_id)
        deadline = float(deadline)
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or remaining > 3.0:
            return 'failed'
        if remaining <= 0:
            return 'timeout'
    except (TypeError, ValueError, OverflowError):
        return 'failed'
    if sys.platform != 'darwin':
        return 'unavailable'
    try:
        bundle = _mac_target()
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, KeyError,
            plistlib.InvalidFileException):
        return 'unavailable'
    if time.monotonic() >= deadline:
        return 'timeout'
    try:
        opener = subprocess.Popen(
            ['/usr/bin/open', '-a', str(bundle), 'codex://threads/' + thread],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False)
    except FileNotFoundError:
        return 'unavailable'
    except OSError:
        return 'failed'
    try:
        result = opener.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        # Parent owns the helper process group and bounds cleanup of both processes.
        try:
            opener.kill()
            opener.wait(timeout=.1)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        return 'timeout'
    if time.monotonic() >= deadline:
        return 'timeout'
    return 'requested' if result == 0 else 'failed'


def _helper_command(thread, deadline):
    if getattr(sys, 'frozen', False):
        helper = Path(sys.executable).with_name('CodexSafeResumeBridge')
        if not helper.is_file():
            raise FileNotFoundError('helper')
        return [str(helper), '--open-chat', thread, str(deadline)]
    return [sys.executable, '-B', str(Path(__file__).with_name('desktop_launcher.py')),
            '--open-chat', thread, str(deadline)]


def _supervise_mac(thread, before_open, deadline=None):
    now = time.monotonic()
    if deadline is None:
        deadline = now + _NAVIGATION_SECONDS
    if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        return 'failed'
    if deadline <= now:
        return 'timeout'
    if deadline - now > 3.0:
        return 'failed'
    def active():
        return before_open is None or before_open() is True
    try:
        if not active():
            return 'cancelled'
        if time.monotonic() >= deadline:
            return 'timeout'
        command = _helper_command(thread, deadline)
        if time.monotonic() >= deadline:
            return 'timeout'
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True, shell=False)
    except FileNotFoundError:
        return 'unavailable'
    except (OSError, ValueError, RuntimeError):
        return 'failed'
    status = 'failed'
    try:
        while True:
            if not active():
                status = 'cancelled'
                break
            if time.monotonic() >= deadline:
                status = 'timeout'
                break
            if child.poll() is not None:
                # Nonblocking, bounded read: an inherited pipe cannot stall the worker.
                os.set_blocking(child.stdout.fileno(), False)
                output = os.read(child.stdout.fileno(), 65)
                text = output.decode('ascii', errors='replace')
                if child.returncode == 0 and text in {item + '\n' for item in _STATUSES}:
                    status = text.rstrip('\n')
                break
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, RuntimeError):
        status = 'failed'
    finally:
        _stop_owned(child, group=True)
        child.stdout.close()
    return status


def open_thread(thread_id, *, before_open=None, deadline=None):
    """Windowsは受付のTrue、Macは要求状態を返す。表示・到着は保証しない。"""
    try:
        thread = check_id(thread_id)
    except (TypeError, ValueError):
        raise ThreadNavigationError('チャットの識別情報を確認できません。') from None
    if sys.platform == 'win32':
        try:
            from windows_thread_navigation import request_open
            return request_open('codex://threads/' + thread, before_open=before_open)
        except (OSError, ValueError, RuntimeError, ImportError, AttributeError, KeyError,
                TypeError, subprocess.SubprocessError):
            raise ThreadNavigationError(_ERROR) from None
    if sys.platform == 'darwin':
        return _supervise_mac(thread, before_open, deadline)
    raise ThreadNavigationError(
        'この端末でチャットを開く方法は未検証です。ChatGPTで対象を開いてください。')
