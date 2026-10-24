"""公式CLIのRAW WebSocket中継をsocketとして扱う。サーバーは起動しない。"""
from contextlib import ExitStack, contextmanager
import errno
import os
from pathlib import Path
import socket
import subprocess
import threading


class NativeProxySocket:
    """所有するproxy子プロセスと、固定長のバイト中継だけを管理する。"""

    _CHUNK_SIZE = 65536
    _CLOSE_TIMEOUT = 0.5

    def __init__(self, executable, timeout=8, codex_home=None):
        executable = os.fspath(executable)
        if not Path(executable).is_absolute():
            raise ValueError('CLIには絶対パスが必要です')
        self._socket, self._relay = socket.socketpair()
        self._process = None
        self._threads = []
        self._close_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._closed = False
        try:
            self._socket.settimeout(timeout)
            options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, shell=False, bufsize=0)
            if codex_home is not None:
                options['env'] = dict(os.environ, CODEX_HOME=os.fspath(codex_home))
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(
                [executable, 'app-server', 'proxy'], **options)
            for target, pipe in ((self._to_proxy, self._process.stdin),
                                 (self._from_proxy, self._process.stdout)):
                self._start_worker(target, pipe)
        except BaseException:
            self.close()
            raise

    def _start_worker(self, target, pipe):
        # 開始失敗とworkerの所有権取得を直列化する。start()が実起動後に
        # 例外を返しても、実行中workerのFDを初期化側では閉じない。
        pending = ExitStack()
        ownership = threading.Lock()
        claimed = cancelled = False

        def run():
            nonlocal claimed
            with ownership:
                if cancelled:
                    return
                claimed = True
            target(fd, relay)

        try:
            fd = os.dup(pipe.fileno())
            pending.callback(os.close, fd)
            pipe.close()  # stdinのEOFをworkerのcloseだけで伝える。
            relay = self._relay.dup()
            pending.callback(relay.close)
            thread = threading.Thread(target=run, daemon=True,
                                      name='native-proxy-relay')
            self._threads.append(thread)
            thread.start()
        except BaseException:
            with ownership:
                if not claimed:
                    cancelled = True
                    pending.close()
            raise
        # ExitStackはGC時にcloseしない。開始後はworkerのfinallyが回収する。
        pending.pop_all()

    def _to_proxy(self, fd, relay):
        try:
            while not self._closed:
                data = relay.recv(self._CHUNK_SIZE)
                if not data:
                    break
                pending = memoryview(data)
                while pending and not self._closed:
                    written = os.write(fd, pending)
                    if written <= 0:
                        return
                    pending = pending[written:]
        except (OSError, ValueError):
            # 閉じたsocket/pipeの例外に本文や認証情報を付けて出力しない。
            pass
        finally:
            os.close(fd)
            self._shutdown(relay, socket.SHUT_RD)
            relay.close()

    def _from_proxy(self, fd, relay):
        try:
            while not self._closed:
                data = os.read(fd, self._CHUNK_SIZE)
                if not data or self._closed:
                    break
                relay.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            os.close(fd)
            # stdout EOFは利用者へ伝えるが、反対方向のEOFは独立させる。
            self._shutdown(relay, socket.SHUT_WR)
            relay.close()

    @staticmethod
    def _shutdown(raw, how):
        try:
            raw.shutdown(how)
        except OSError:
            pass

    @contextmanager
    def _operation_socket(self):
        # blocking I/O中はlockを持たず、closeのshutdownで解除できる。
        with self._io_lock:
            if self._closed:
                raise OSError(errno.EBADF, 'proxy socket is closed')
            raw = self._socket.dup()
        try:
            yield raw
        finally:
            raw.close()

    def send(self, data, flags=0):
        with self._operation_socket() as raw:
            return raw.send(data, flags)

    def sendall(self, data, flags=0):
        with self._operation_socket() as raw:
            return raw.sendall(data, flags)

    def recv(self, size, flags=0):
        with self._operation_socket() as raw:
            return raw.recv(size, flags)

    def settimeout(self, timeout):
        with self._io_lock:
            return self._socket.settimeout(timeout)

    def gettimeout(self):
        with self._io_lock:
            return self._socket.gettimeout()

    def fileno(self):
        with self._io_lock:
            return self._socket.fileno()

    def getsockopt(self, *args):
        with self._io_lock:
            return self._socket.getsockopt(*args)

    def setsockopt(self, *args):
        with self._io_lock:
            return self._socket.setsockopt(*args)

    def getpeername(self):
        with self._io_lock:
            return self._socket.getpeername()

    def getsockname(self):
        with self._io_lock:
            return self._socket.getsockname()

    def shutdown(self, how):
        with self._operation_socket() as raw:
            return raw.shutdown(how)

    def close(self):
        """中継を解除し、所有するproxyだけを期限付きで停止・回収する。"""
        with self._close_lock:
            with self._io_lock:
                if self._closed:
                    return
                self._closed = True
                self._shutdown(self._socket, socket.SHUT_RDWR)
            self._shutdown(self._relay, socket.SHUT_RDWR)
            try:
                if self._process is not None:
                    if self._process.poll() is None:
                        try:
                            self._process.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            self._process.wait(timeout=self._CLOSE_TIMEOUT)
                        except subprocess.TimeoutExpired:
                            self._process.kill()
                            self._process.wait(timeout=self._CLOSE_TIMEOUT)
                    else:
                        self._process.wait(timeout=self._CLOSE_TIMEOUT)
            finally:
                # 初期化途中の元pipeも回収する。実行中workerの複製FDは
                # 閉じず、子の終了とsocket shutdownでI/Oを解除する。
                if self._process is not None:
                    for pipe in (self._process.stdin, self._process.stdout):
                        if pipe is not None:
                            pipe.close()
                with self._io_lock:
                    self._socket.close()
                self._relay.close()
                for thread in self._threads:
                    if thread.ident is not None:
                        thread.join(timeout=self._CLOSE_TIMEOUT)
            if any(thread.is_alive() for thread in self._threads):
                raise RuntimeError('proxy中継の終了を確認できません')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
