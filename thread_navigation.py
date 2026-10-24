"""起動中の公式Desktopへチャットを開く要求だけを渡す。"""
from pathlib import Path
import ctypes
import plistlib
import subprocess
import sys

from cli_security import validate_location
from core import check_id, reject_links
from desktop_binding import desktop_bundle, process_identity
from queue_platform import DARWIN_TEAM


class ThreadNavigationError(RuntimeError):
    pass


# AppleEvents.h: kAEDoNotPromptForUserConsent. NeverInteract alone does not suppress TCC.
_NO_USER_CONSENT_PROMPT = 0x00020000

_ERROR = 'チャットを開く要求を確認できません。Codexで対象を開いてください。'


class _Address(ctypes.Structure):
    _pack_ = 2
    _fields_ = [('descriptor_type', ctypes.c_uint32), ('data_handle', ctypes.c_void_p)]


def _event_permission(pid):
    """起動済みPID宛てのURLイベントだけを、権限要求なしで照会する。"""
    library = ctypes.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
    library.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long,
                                   ctypes.POINTER(_Address)]
    library.AECreateDesc.restype = ctypes.c_int16
    library.AEDisposeDesc.argtypes = [ctypes.POINTER(_Address)]
    library.AEDisposeDesc.restype = ctypes.c_int16
    library.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(_Address),
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_ubyte]
    library.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
    target, address = ctypes.c_int32(pid), _Address()
    if library.AECreateDesc(int.from_bytes(b'kpid', 'big'), ctypes.byref(target),
                            ctypes.sizeof(target), ctypes.byref(address)) != 0:
        return False
    try:
        return library.AEDeterminePermissionToAutomateTarget(
            ctypes.byref(address), int.from_bytes(b'GURL', 'big'),
            int.from_bytes(b'GURL', 'big'), False) == 0
    finally:
        library.AEDisposeDesc(ctypes.byref(address))


def _mac_target():
    import AppKit

    applications = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
        'com.openai.codex')
    running = [application for application in applications if not application.isTerminated()]
    if len(running) != 1:
        raise ValueError('running_desktop')
    identity = process_identity(int(running[0].processIdentifier()))
    executable = Path(identity['executable'])
    if (not executable.is_absolute() or '..' in executable.parts
            or executable.parent.name != 'MacOS' or executable.parent.parent.name != 'Contents'):
        raise ValueError('executable')
    bundle = executable.parent.parent.parent
    if bundle.suffix != '.app' or desktop_bundle(identity['pid']) != 'com.openai.codex':
        raise ValueError('bundle')
    validate_location(executable)
    plist = bundle / 'Contents/Info.plist'
    reject_links(plist)
    if plist.stat().st_size > 1024 * 1024:
        raise ValueError('plist_size')
    info = plistlib.loads(plist.read_bytes())
    schemes = [scheme for item in info.get('CFBundleURLTypes', [])
               for scheme in item.get('CFBundleURLSchemes', [])]
    if (info.get('CFBundleIdentifier') != 'com.openai.codex'
            or info.get('CFBundleExecutable') != executable.name or 'codex' not in schemes):
        raise ValueError('uri_support')
    requirement = ('anchor apple generic and identifier "com.openai.codex" '
                   'and certificate leaf[subject.OU] = "' + DARWIN_TEAM + '"')
    result = subprocess.run(
        ['/usr/bin/codesign', '--verify', '--strict', '--deep', '-R', '=' + requirement, str(bundle)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=10, shell=False)
    if result.returncode != 0 or process_identity(identity['pid']) != identity:
        raise ValueError('desktop_changed')
    return bundle, identity


def open_thread(thread_id, *, before_open=None):
    """受付成功だけを返す。到着やメッセージ送信の成功を意味しない。"""
    try:
        thread = check_id(thread_id)
    except (TypeError, ValueError):
        raise ThreadNavigationError('チャットの識別情報を確認できません。') from None
    if sys.platform != 'darwin':
        raise ThreadNavigationError(
            'この端末でチャットを開く方法は未検証です。Codexで対象を開いてください。')
    try:
        _bundle, identity = _mac_target()
        if not _event_permission(identity['pid']):
            raise ValueError('event_permission')
        import AppKit
        import Foundation
        descriptors = Foundation.NSAppleEventDescriptor
        target = descriptors.descriptorWithProcessIdentifier_(identity['pid'])
        event = descriptors.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
            int.from_bytes(b'GURL', 'big'), int.from_bytes(b'GURL', 'big'), target, -1, 0)
        event.setParamDescriptor_forKeyword_(
            descriptors.descriptorWithString_('codex://threads/' + thread), int.from_bytes(b'----', 'big'))
        if before_open is not None and before_open() is not True:
            raise ValueError('cancelled')
        if process_identity(identity['pid']) != identity:
            raise ValueError('desktop_changed')
        # Address the existing process directly. A dead recipient cannot launch another app.
        _reply, error = event.sendEventWithOptions_timeout_error_(
            Foundation.NSAppleEventSendNoReply | Foundation.NSAppleEventSendNeverInteract
            | _NO_USER_CONSENT_PROMPT, 5, None)
        if error is not None or process_identity(identity['pid']) != identity:
            raise ValueError('open_not_confirmed')
        if before_open is None or before_open() is True:
            application = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(identity['pid'])
            if (application is None or application.isTerminated()
                    or not application.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)):
                raise ValueError('activation_not_confirmed')
    except (OSError, ValueError, RuntimeError, ImportError, AttributeError, KeyError,
            TypeError, subprocess.SubprocessError):
        raise ThreadNavigationError(_ERROR) from None
    return True
