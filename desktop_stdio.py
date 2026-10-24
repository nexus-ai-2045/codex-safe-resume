"""DesktopのJSON行を、既存の公式proxyへ双方向中継する。"""
import io
from contextlib import nullcontext
import json
import math
import os
import select
import socket
import sys
import threading

import websocket
from websocket._abnf import continuous_frame, frame_buffer

from native_proxy import NativeProxySocket


# stdioでは区切りのLFを含める。WebSocketも本文の読取り前に制限する。
MAX_FRAME_BYTES = 16 * 1024 * 1024
_READ_CHUNK = 65536
_POLL_SECONDS = 0.05


class DesktopStdioError(RuntimeError):
    """本文や認証情報を含まない中継エラー。"""

    REASONS = frozenset(('stdin_read', 'stdin_frame', 'websocket_send', 'websocket_read',
                         'websocket_frame', 'stdout_write', 'cleanup', 'relay_preparation'))

    def __init__(self, *args, reason='relay_preparation'):
        super().__init__(*args)
        self.reason = reason if type(reason) is str and reason in self.REASONS else 'cleanup'


class RelayPreparationError(DesktopStdioError):
    pass


class _BoundedFrameBuffer(frame_buffer):
    def recv_length(self):
        super().recv_length()
        if self.length > MAX_FRAME_BYTES:
            raise DesktopStdioError('Desktop中継のフレームが上限を超えました', reason='websocket_frame')


class _BoundedContinuousFrame(continuous_frame):
    def add(self, frame):
        previous = len(self.cont_data[1]) if self.cont_data else 0
        if previous + len(frame.data) > MAX_FRAME_BYTES:
            raise DesktopStdioError('Desktop中継のフレームが上限を超えました', reason='websocket_frame')
        super().add(frame)


class _BoundedWebSocket(websocket.WebSocket):
    def __init__(self):
        super().__init__(skip_utf8_validation=False, enable_multithread=True)
        # websocket-clientの既存フレーム処理に、確保前の上限だけを加える。
        self.frame_buffer = _BoundedFrameBuffer(self._recv, False)
        self.cont_frame = _BoundedContinuousFrame(False, False)


class _InputReader:
    """buffered stdinのblocking threadを残さず、EOFと停止を観測する。"""

    def __init__(self, stream):
        self.stream = stream
        self.memory = isinstance(stream, io.BytesIO)
        self.fd = None if self.memory else stream.fileno()
        self.windows_pipe = False
        if not self.memory and os.name == 'nt':
            import ctypes
            import msvcrt
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.handle = wintypes.HANDLE(msvcrt.get_osfhandle(self.fd))
            self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
            self.kernel.GetFileType.restype = wintypes.DWORD
            kind = self.kernel.GetFileType(self.handle)
            if kind not in (1, 3):  # FILE_TYPE_DISK / FILE_TYPE_PIPE
                raise DesktopStdioError('Desktop中継にはバイナリのstdin pipeが必要です')
            self.windows_pipe = kind == 3
            self.available_type = wintypes.DWORD
            self.kernel.PeekNamedPipe.argtypes = [
                wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            self.kernel.PeekNamedPipe.restype = wintypes.BOOL

    def read(self, limit, stopped):
        if self.memory:
            return self.stream.read(limit)
        if os.name == 'nt':
            if self.windows_pipe:
                import ctypes

                available = self.available_type()
                if not self.kernel.PeekNamedPipe(
                        self.handle, None, 0, None, ctypes.byref(available), None):
                    if ctypes.get_last_error() in (109, 232, 233):
                        return b''  # broken / closing / disconnected pipe
                    raise OSError('stdin pipeの読取りを確認できません')
                if not available.value:
                    stopped.wait(_POLL_SECONDS)
                    return None
                limit = min(limit, available.value)
        elif not select.select([self.fd], [], [], _POLL_SECONDS)[0]:
            return None
        return os.read(self.fd, limit)


class _OutputWriter:
    """単一の書手で非blocking出力し、元のpipe modeを終了時に戻す。"""

    def __init__(self, stream):
        self.stream = stream
        self.memory = isinstance(stream, io.BytesIO)
        self.fd = None if self.memory else stream.fileno()
        self.previous_mode = None
        self.windows_pipe = False
        if self.memory:
            return
        if os.name != 'nt':
            self.previous_mode = os.get_blocking(self.fd)
            os.set_blocking(self.fd, False)
            return

        # Python 3.11のos.set_blockingはWindows pipeに未対応。
        # SetNamedPipeHandleStateは匿名pipeにも使え、WriteFileは
        # PIPE_NOWAITなら短いwriteまたは0 bytesで直ちに戻る。
        import ctypes
        import msvcrt
        from ctypes import wintypes

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.handle = wintypes.HANDLE(msvcrt.get_osfhandle(self.fd))
        self.count_type = wintypes.DWORD
        self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
        self.kernel.GetFileType.restype = wintypes.DWORD
        kind = self.kernel.GetFileType(self.handle)
        if kind not in (1, 3):  # FILE_TYPE_DISK / FILE_TYPE_PIPE
            raise DesktopStdioError('Desktop中継にはバイナリのstdout pipeが必要です')
        self.kernel.WriteFile.argtypes = [
            wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        self.kernel.WriteFile.restype = wintypes.BOOL
        self.windows_pipe = kind == 3
        if self.windows_pipe:
            self.kernel.GetNamedPipeHandleStateW.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
                wintypes.LPWSTR, wintypes.DWORD]
            self.kernel.GetNamedPipeHandleStateW.restype = wintypes.BOOL
            self.kernel.SetNamedPipeHandleState.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID, wintypes.LPVOID]
            self.kernel.SetNamedPipeHandleState.restype = wintypes.BOOL
            mode = wintypes.DWORD()
            if not self.kernel.GetNamedPipeHandleStateW(
                    self.handle, ctypes.byref(mode), None, None, None, None, 0):
                raise OSError('stdout pipeの状態を確認できません')
            self._set_windows_mode(mode.value | 1)  # PIPE_NOWAIT
            self.previous_mode = mode.value

    def _set_windows_mode(self, value):
        import ctypes

        mode = self.count_type(value)
        if not self.kernel.SetNamedPipeHandleState(
                self.handle, ctypes.byref(mode), None, None):
            raise OSError('stdout pipeの待機方式を設定できません')

    def write(self, pending):
        if self.memory:
            return self.stream.write(pending)
        pending = pending[:_READ_CHUNK]
        if os.name == 'nt':
            import ctypes

            count = self.count_type()
            if not self.kernel.WriteFile(self.handle, bytes(pending), len(pending),
                                         ctypes.byref(count), None):
                raise OSError('stdout pipeへの出力を確認できません')
            return None if self.windows_pipe and not count.value else count.value
        try:
            return os.write(self.fd, pending)
        except BlockingIOError:
            return None

    def close(self):
        # 呼出元のstreamは所有しない。buffered flushは行わずmodeだけ戻す。
        if self.previous_mode is not None:
            if os.name == 'nt':
                self._set_windows_mode(self.previous_mode)
            else:
                os.set_blocking(self.fd, self.previous_mode)
            self.previous_mode = None


def _reject_constant(_):
    raise ValueError('JSONの定数形式を確認できません')


def _json_object(payload):
    if not isinstance(payload, bytes) or len(payload) > MAX_FRAME_BYTES:
        raise ValueError('JSONのフレーム形式を確認できません')
    text = payload.decode('utf-8')
    if not isinstance(json.loads(text, parse_constant=_reject_constant), dict):
        raise ValueError('JSONオブジェクトが必要です')
    return text


def _write_all(stream, payload, stopped, *, receipt=None, message=None):
    pending = memoryview(payload)
    while pending:
        if stopped.is_set():
            return False
        # The writer is nonblocking. Hold the receipt lock for one write only,
        # never during backpressure waits. The final byte and delivery proof
        # are atomic relative to forwarding initialized on the input thread.
        with receipt.lock if receipt is not None else nullcontext():
            written = stream.write(pending)
            if receipt is not None and written == len(pending):
                receipt.delivered(message)
        if written is None:
            stopped.wait(_POLL_SECONDS)
            continue
        if not isinstance(written, int) or written <= 0 or written > len(pending):
            raise OSError('stdoutへの出力を確認できません')
        pending = pending[written:]
    return True


def _partial_frame(ws):
    return bool(ws.frame_buffer.header is not None or ws.frame_buffer.recv_buffer
                or ws.cont_frame.cont_data)


def relay(executable, codex_home=None, stdin=None, stdout=None, timeout=8, receipt=None):
    """公式proxyのみを所有し、initializeや承認応答を生成せず中継する。

    executableの信頼確認と共有サーバーの起動は呼出元の責務。
    stdinはバイナリpipe・ファイルまたはBytesIO、stdoutはバイナリ出力。
    stdoutは他の書手や未flushデータがない専用streamを渡す。
    Windowsではstdinの読取り可能量を確認し、stdoutはPIPE_NOWAITを使う。
    """
    raw = None
    ws = None
    sender = None
    output = None
    stopped = threading.Event()
    input_failed = threading.Event()
    failed = False
    input_started = False
    cleanup_failed = False
    failure_reason = None
    input_reason = None
    phase = 'relay_preparation'
    try:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('接続期限は正の有限秒数が必要です')
        source = _InputReader(sys.stdin.buffer if stdin is None else stdin)
        output = _OutputWriter(sys.stdout.buffer if stdout is None else stdout)
        # ライブラリの任意のtrace設定にも本文や認証RPCを残さない。
        websocket.enableTrace(False)
        raw = NativeProxySocket(executable, timeout=timeout, codex_home=codex_home)
        ws = _BoundedWebSocket()
        expired = threading.Event()

        def expire_handshake():
            expired.set()
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # socketの一回ごとのtimeoutだけでは、少量ずつ届くhandshakeを
        # 無期限に待ち得るため、handshake全体にも同じ期限を適用する。
        watchdog = threading.Timer(timeout, expire_handshake)
        watchdog.daemon = True
        watchdog.start()
        try:
            ws.connect('ws://localhost/rpc', socket=raw, timeout=timeout)
        finally:
            watchdog.cancel()
            watchdog.join()
        if expired.is_set():
            raise ValueError('共有proxyへの接続が期限を超えました')
        ws.settimeout(None)

        def incoming():
            nonlocal input_reason
            pending = bytearray()
            input_phase = 'stdin_read'
            try:
                while not stopped.is_set():
                    input_phase = 'stdin_read'
                    chunk = source.read(min(_READ_CHUNK, MAX_FRAME_BYTES + 1 - len(pending)),
                                        stopped)
                    input_phase = 'stdin_frame'
                    if chunk is None:
                        continue
                    if not chunk:
                        if pending:
                            raise ValueError('未終端のJSON行を拒否しました')
                        break
                    pending.extend(chunk)
                    while b'\n' in pending:
                        boundary = pending.index(b'\n') + 1
                        if boundary > MAX_FRAME_BYTES:
                            raise ValueError('JSON行が上限を超えました')
                        payload = bytes(pending[:boundary - 1])
                        del pending[:boundary]
                        text = _json_object(payload)
                        if stopped.is_set():
                            return
                        # Serialize request observation with response observation:
                        # a fast peer cannot answer before its request ID is recorded.
                        with receipt.lock if receipt is not None else nullcontext():
                            input_phase = 'websocket_send'
                            ws.send(text)
                            if receipt is not None:
                                receipt.forwarded(json.loads(text))
                        input_phase = 'stdin_frame'
                    if len(pending) >= MAX_FRAME_BYTES:
                        raise ValueError('JSON行が上限を超えました')
            except Exception:
                if not stopped.is_set():
                    input_reason = input_phase
                    input_failed.set()
            finally:
                stopped.set()
                try:
                    ws.abort()
                except Exception:
                    if not input_failed.is_set():
                        input_reason = 'cleanup'
                        input_failed.set()

        sender = threading.Thread(target=incoming, name='desktop-stdio-input')
        input_started = True
        sender.start()
        # stdoutの書手は常にこのスレッドだけ。短いwriteも完了させる。
        while not stopped.is_set():
            phase = 'websocket_read'
            try:
                opcode, payload = ws.recv_data()
            except websocket.WebSocketTimeoutException:
                # handshake以降に発生した一時的なidleは切断理由にしない。
                continue
            except websocket.WebSocketConnectionClosedException:
                phase = 'websocket_frame'
                if not stopped.is_set() and _partial_frame(ws):
                    raise ValueError('未終端のWebSocketフレームを拒否しました') from None
                break
            phase = 'websocket_frame'
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            if opcode != websocket.ABNF.OPCODE_TEXT:
                raise ValueError('JSONテキスト以外のフレームを拒否しました')
            text = _json_object(payload)
            # JSON文字列内の改行はescape済み。外側の整形改行だけを除く。
            line = text.replace('\r', '').replace('\n', '').encode('utf-8') + b'\n'
            if len(line) > MAX_FRAME_BYTES:
                raise ValueError('JSON行が上限を超えました')
            phase = 'stdout_write'
            if not _write_all(output, line, stopped, receipt=receipt,
                              message=json.loads(text) if receipt is not None else None):
                break
    except Exception as exc:
        failed = True
        failure_reason = (exc.reason if isinstance(exc, DesktopStdioError)
                          and exc.reason != 'relay_preparation' else phase)
    finally:
        stopped.set()
        if receipt is not None:
            try:
                receipt.close()
            except Exception:
                failed = True
                cleanup_failed = True
        if ws is not None:
            try:
                ws.abort()
            except Exception:
                failed = True
                cleanup_failed = True
        if raw is not None:
            try:
                raw.close()
            except Exception:
                failed = True
                cleanup_failed = True
        if sender is not None and sender.ident is not None:
            sender.join(timeout=1)
            if sender.is_alive():
                failed = True
                cleanup_failed = True
        if output is not None:
            try:
                output.close()
            except Exception:
                failed = True
                cleanup_failed = True
    if failed or input_failed.is_set():
        if not input_started and not cleanup_failed:
            raise RelayPreparationError('標準入力を読む前に共有接続の準備が失敗しました',
                                        reason='relay_preparation') from None
        reason = input_reason if input_failed.is_set() else failure_reason or 'cleanup'
        raise DesktopStdioError('Desktop中継を継続できません。接続とフレーム形式を確認してください',
                                reason=reason) from None
