"""公式stdio RPCで利用枠だけを読み、表示用の匿名データを返す。

認証・設定ファイルを直接読まず、公式CLIに通常のアカウント解決を任せる。
送信能力を持たず、返却値を自動再開の許可判定に使ってはいけない。
"""
from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
from typing import BinaryIO

from cli_security import validate_location
from core import quota_bucket
from plan_metadata import PLAN_LABELS


MAX_LINE_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 65536
_MAX_NOTIFICATIONS = 32
# Two replies, each preceded by the allowed notification burst, plus EOF.
_MAX_PENDING_LINES = 2 * (_MAX_NOTIFICATIONS + 1) + 1
_MAX_DURATION_MINUTES = 10 * 366 * 24 * 60
_MIN_RESET_EPOCH = 946684800  # 2000-01-01 UTC
_MAX_RESET_EPOCH = 4102444800  # 2100-01-01 UTC
_CLOSE_TIMEOUT = 0.25
_ERROR = '利用枠を確認できません。公式CLIと接続状態を確認してください。'


class UsageReadError(RuntimeError):
    """本文・アカウント識別子・配置先を含まない利用枠読取りエラー。"""


def summarize_limits(reply: object) -> dict[str, object]:
    """公式の枠応答を表示用へ縮約し、ID・メール・任意文字列を返さない。"""
    if not isinstance(reply, dict):
        raise ValueError('invalid response')
    # RPC response only; legacy direct-window fixtures are not this interface.
    bucket = quota_bucket(reply, require_view=True)
    windows = []
    for slot in ('primary', 'secondary'):
        window = bucket.get(slot)
        if window is None:
            continue
        if not isinstance(window, dict):
            raise ValueError('invalid window')
        used = window.get('usedPercent')
        if (isinstance(used, bool) or not isinstance(used, (int, float))
                or not 0 <= used <= 100 or not math.isfinite(used)):
            raise ValueError('invalid percentage')
        duration = window.get('windowDurationMins')
        if duration is not None and (type(duration) is not int
                                    or not 0 < duration <= _MAX_DURATION_MINUTES):
            raise ValueError('invalid duration')
        resets = window.get('resetsAt')
        if resets is not None and (type(resets) is not int
                                  or not _MIN_RESET_EPOCH <= resets <= _MAX_RESET_EPOCH):
            raise ValueError('invalid reset')
        windows.append(dict(slot=slot, duration_minutes=duration, used_percent=used,
                            remaining_percent=100 - used, resets_at=resets))
    if not windows:
        raise ValueError('missing windows')
    account = reply.get('accountId')
    ordinary = reply.get('ordinaryUsageAllowed')
    plan = bucket.get('planType')
    return dict(checked_at=int(time.time()), windows=windows,
                account_bound=isinstance(account, str) and bool(account.strip()),
                ordinary_usage_allowed=ordinary if type(ordinary) is bool else None,
                plan_type=plan if isinstance(plan, str) and plan in PLAN_LABELS else None)


def _collect_lines(stream: BinaryIO, incoming: queue.Queue[bytes | None],
                   failed: threading.Event) -> None:
    pending = bytearray()
    try:
        while chunk := stream.read(_READ_CHUNK):
            if not isinstance(chunk, bytes):
                raise ValueError('invalid stream')
            pending.extend(chunk)
            while b'\n' in pending:
                boundary = pending.index(b'\n') + 1
                if boundary > MAX_LINE_BYTES:
                    raise ValueError('large line')
                incoming.put_nowait(bytes(pending[:boundary - 1]))
                del pending[:boundary]
            if len(pending) >= MAX_LINE_BYTES:
                raise ValueError('large line')
        if pending:
            raise ValueError('unterminated line')
    except Exception:
        failed.set()
    finally:
        try:
            incoming.put_nowait(None)
        except queue.Full:
            failed.set()


def _reject_constant(_: str) -> None:
    raise ValueError('invalid JSON constant')


def _response(incoming: queue.Queue[bytes | None], failed: threading.Event,
              ident: int, deadline: float, cancel: threading.Event | None = None) -> dict:
    for _ in range(_MAX_NOTIFICATIONS + 1):
        while True:
            if cancel is not None and cancel.is_set():
                raise UsageReadError(_ERROR)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or failed.is_set():
                raise ValueError('response unavailable')
            try:
                line = incoming.get(timeout=min(remaining, 0.1))
                break
            except queue.Empty:
                continue
        if line is None:
            raise ValueError('stream closed')
        message = json.loads(line.decode('utf-8'), parse_constant=_reject_constant)
        if not isinstance(message, dict):
            raise ValueError('invalid message')
        if 'method' in message:
            # 承認・入力・認証を含むserver requestには一切応答しない。
            if 'id' in message:
                raise ValueError('server request')
            continue
        if (type(message.get('id')) is not int or message['id'] != ident
                or 'error' in message or not isinstance(message.get('result'), dict)):
            raise ValueError('invalid response')
        return message['result']
    raise ValueError('notification flood')


def _send(stream: BinaryIO, message: dict) -> None:
    if message.get('method') not in ('initialize', 'initialized', 'account/rateLimits/read'):
        raise ValueError('unapproved method')
    pending = memoryview((json.dumps(message) + '\n').encode())
    while pending:
        written = stream.write(pending)
        if not isinstance(written, int) or written <= 0 or written > len(pending):
            raise OSError('write failed')
        pending = pending[written:]
    stream.flush()


def _close(process: subprocess.Popen | None, reader: threading.Thread | None) -> bool:
    """stdin EOF後に所有childだけを段階的に停止し、pipeとreaderを回収する。"""
    clean = True
    if process is not None:
        try:
            process.stdin.close()
        except Exception:
            clean = False
        for step in range(3):
            try:
                if step == 1 and process.poll() is None:
                    process.terminate()
                elif step == 2 and process.poll() is None:
                    process.kill()
                process.wait(timeout=_CLOSE_TIMEOUT)
                break
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                clean = False
        if process.poll() is None:
            clean = False
        try:
            process.stdout.close()
        except Exception:
            clean = False
    if reader is not None and reader.ident is not None:
        reader.join(timeout=1)
        clean = clean and not reader.is_alive()
    return clean


def read_limits(executable: str | os.PathLike[str], timeout: float = 8, *,
                peer_reply: object = None,
                cancel: threading.Event | None = None) -> dict[str, object]:
    """検証したCLIから利用枠を一度読み、識別情報を除いた表示用結果を返す。

    timeoutはstdio交換全体の期限。配置検査と終了処理は各自の有限期限を持つ。
    cancelは表示要求の中止通知。応答待ちを中断しても所有childとreaderを回収する。
    primary/secondaryは位置であり、5時間枠・週次枠の意味はduration_minutesで読む。
    省略・nullのduration/resetはNoneのまま保持し、過去のresetも推測で更新しない。
    peer_replyは共有接続の読取り結果。識別子はメモリ内で比較し、一致の真偽だけ返す。
    一致は取得元同士の今回の観測であり、Desktop本人確認・送信許可ではない。
    """
    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise UsageReadError(_ERROR)

    process = None
    reader = None
    result = None
    failed = False
    try:
        check_cancel()
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('invalid timeout')
        path = validate_location(executable)
        check_cancel()
        deadline = time.monotonic() + timeout
        options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, shell=False, bufsize=0)
        if os.name == 'nt':
            options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        process = subprocess.Popen([str(path), 'app-server', '--listen', 'stdio://'], **options)
        incoming: queue.Queue[bytes | None] = queue.Queue(maxsize=_MAX_PENDING_LINES)
        reader_failed = threading.Event()
        reader = threading.Thread(target=_collect_lines,
                                  args=(process.stdout, incoming, reader_failed),
                                  daemon=True, name='usage-limits-reader')
        reader.start()
        _send(process.stdin, {'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'codex_safe_resume_usage', 'version': '0.5.0'}}})
        initialized = _response(incoming, reader_failed, 1, deadline, cancel)
        if not isinstance(initialized.get('userAgent'), str) or not initialized['userAgent']:
            raise ValueError('invalid initialization')
        check_cancel()
        _send(process.stdin, {'method': 'initialized', 'params': {}})
        _send(process.stdin, {'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
        reply = _response(incoming, reader_failed, 2, deadline, cancel)
        check_cancel()
        result = summarize_limits(reply)
        result.update(peer_status='not_provided', peer_limits=None, account_match=None)
        if peer_reply is not None:
            try:
                peer = summarize_limits(peer_reply)
            except (ValueError, TypeError):
                result['peer_status'] = 'invalid'
            else:
                result.update(peer_status='read', peer_limits=peer)
                if result['account_bound'] and peer['account_bound']:
                    result['account_match'] = reply['accountId'] == peer_reply['accountId']
    except Exception:
        failed = True
    finally:
        try:
            clean = _close(process, reader)
        except Exception:
            clean = False
    if failed or not clean or result is None:
        raise UsageReadError(_ERROR) from None
    return result
