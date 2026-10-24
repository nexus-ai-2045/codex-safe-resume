from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from connection_settings import _fingerprint
from usage_client import _close, _collect_lines, _response


_OUTPUT_LIMIT = 65536
_ERROR = '公式CLIのキュー接続を確認できません。'
_VERSION = re.compile(rb'codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\s*')


class QueueTransportError(RuntimeError):
    pass


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('invalid_identifier')
    return value


class _BoundedStream:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = _OUTPUT_LIMIT

    def read(self, size):
        chunk = self.stream.read(min(size, self.remaining + 1))
        if not isinstance(chunk, bytes) or len(chunk) > self.remaining:
            raise ValueError('output_limit')
        self.remaining -= len(chunk)
        return chunk


class QueueTransport:
    def __init__(self, executable, codex_home, timeout=8):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise QueueTransportError(_ERROR)
        self.executable = Path(executable)
        self.codex_home = Path(codex_home)
        self.timeout = timeout
        self._verified = None
        self._owned = set()

    def _environment(self):
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith('CSR_') and key.upper() not in (
                   'CODEX_CLI_PATH', 'CODEX_APP_SERVER_FORCE_CLI')}
        env['CODEX_HOME'] = str(self.codex_home)
        return env

    def _options(self):
        options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, shell=False, close_fds=True, bufsize=0,
                       env=self._environment())
        if os.name == 'nt':
            options['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        return options

    def _identity(self):
        if not self.codex_home.is_absolute() or not self.codex_home.is_dir():
            raise ValueError('invalid_home')
        path, digest = _fingerprint(self.executable)
        with path.open('rb') as stream:
            magic = stream.read(4)
        if os.name == 'nt':
            native = magic[:2] == b'MZ'
        elif sys.platform == 'darwin':
            native = magic in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe',
                               b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                               b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
                               b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca')
        else:
            native = os.name == 'posix' and magic == b'\x7fELF'
        if not native:
            raise ValueError('not_native')
        return path, digest

    def _ready(self):
        return self._verified is not None and self._identity() == self._verified

    def _reader(self, process, bounded=True):
        incoming = queue.Queue(maxsize=_OUTPUT_LIMIT + 1 if bounded else 67)
        failed = threading.Event()
        stream = _BoundedStream(process.stdout) if bounded else process.stdout
        reader = threading.Thread(target=_collect_lines, args=(stream, incoming, failed),
                                  daemon=True, name='queue-cli-reader')
        reader.start()
        return incoming, failed, reader

    def _output(self, arguments):
        process = None
        reader = None
        try:
            process = subprocess.Popen([str(self.executable), *arguments], **self._options())
            incoming, failed, reader = self._reader(process)
            deadline = time.monotonic() + self.timeout
            chunks = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or failed.is_set():
                    raise ValueError('output_unavailable')
                line = incoming.get(timeout=remaining)
                if line is None:
                    break
                chunks.append(line)
            if failed.is_set():
                raise ValueError('output_unavailable')
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            return status, b'\n'.join(chunks)
        finally:
            if not self._finish(process, reader):
                raise QueueTransportError(_ERROR)

    def check(self):
        self._verified = None
        try:
            identity = self._identity()
            status, version = self._output(['--version'])
            if status != 0 or _VERSION.fullmatch(version) is None:
                return False
            status, help_text = self._output(['queue', '--help'])
            if (status != 0 or b'Usage:' not in help_text or b'queue' not in help_text
                    or re.search(rb'(?<!\S)--thread(?:[ =]|$)', help_text) is None
                    or re.search(rb'(?<!\S)--message(?:[ =]|$)', help_text) is None
                    or self._identity() != identity):
                return False
            self._verified = identity
            return True
        except Exception:
            return False

    def submit(self, thread_id, text, attempt_id, before_send=None):
        process = None
        reader = None
        try:
            _uuid(thread_id)
            _uuid(attempt_id)
            if (not isinstance(text, str) or not text or len(text) > 8192 or '\0' in text
                    or not self._ready()):
                return {'outcome': 'not_started'}
            options = self._options()
            arguments = [str(self._verified[0]), 'queue', '--thread', thread_id, '--message=' + text]
            if before_send is not None and before_send() is not True:
                return {'outcome': 'not_started'}
            process = subprocess.Popen(arguments, **options)
        except Exception:
            return {'outcome': 'not_started' if process is None else 'unknown'}
        outcome = {'outcome': 'unknown'}
        try:
            incoming, failed, reader = self._reader(process)
            deadline = time.monotonic() + self.timeout
            line = incoming.get(timeout=self.timeout)
            final = incoming.get(timeout=max(0.001, deadline - time.monotonic()))
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            if line is not None and final is None and not failed.is_set() and status == 0:
                match = re.fullmatch(rb'Queued message ([0-9a-f-]{36}) for thread '
                                     + thread_id.encode('ascii') + rb'\.', line)
                if match is not None:
                    queue_id = _uuid(match[1].decode('ascii'))
                    self._owned.add((thread_id, queue_id))
                    outcome = {'outcome': 'accepted', 'queue_id': queue_id}
        except Exception:
            pass
        finally:
            if not self._finish(process, reader):
                outcome = {'outcome': 'unknown'}
        return outcome

    def _params(self, method, params):
        if not isinstance(params, dict):
            raise ValueError('invalid_params')
        if method in ('account/read', 'account/rateLimits/read') and params == {}:
            return
        if method == 'thread/queue/list' and set(params) == {'threadId'}:
            _uuid(params['threadId'])
            return
        if method == 'thread/queue/delete' and set(params) == {'threadId', 'queuedSubmissionId'}:
            thread = _uuid(params['threadId'])
            receipt = _uuid(params['queuedSubmissionId'])
            if (thread, receipt) in self._owned:
                return
        raise ValueError('unapproved_method')

    def rpc(self, method, params):
        process = None
        reader = None
        writers = []
        result = None
        good = False
        try:
            self._params(method, params)
            if not self._ready():
                raise ValueError('unverified_cli')
            deadline = time.monotonic() + self.timeout
            process = subprocess.Popen([str(self._verified[0]), 'app-server', '--listen', 'stdio://'],
                                       **self._options())
            incoming, failed, reader = self._reader(process, bounded=False)
            initialize = dict(id=1, method='initialize', params={
                'clientInfo': {'name': 'codex_safe_resume_queue', 'version': '0.5.0'},
                'capabilities': {'experimentalApi': True}})
            self._write(process, initialize, deadline, writers)
            initialized = _response(incoming, failed, 1, deadline)
            if not isinstance(initialized.get('userAgent'), str) or not initialized['userAgent']:
                raise ValueError('invalid_initialize')
            self._write(process, {'method': 'initialized', 'params': {}}, deadline, writers)
            self._write(process, {'id': 2, 'method': method, 'params': params}, deadline, writers)
            result = _response(incoming, failed, 2, deadline)
            good = True
        except Exception:
            pass
        finally:
            good = self._finish(process, reader, writers) and good
        if not good:
            raise QueueTransportError(_ERROR) from None
        if method == 'thread/queue/delete' and result.get('deleted') is True:
            self._owned.discard((params['threadId'], params['queuedSubmissionId']))
        return result

    def enqueue(self, thread_id, text, attempt_id, before_send=None):
        process = None
        reader = None
        writers = []
        dispatched = False
        outcome = {'outcome': 'not_started'}
        try:
            _uuid(thread_id)
            _uuid(attempt_id)
            if (not isinstance(text, str) or not text or len(text) > 8192 or '\0' in text
                    or not self._ready()):
                return outcome
            params = {'threadId': thread_id, 'input': [{'type': 'text', 'text': text}],
                      'clientUserMessageId': attempt_id}
            deadline = time.monotonic() + self.timeout
            process = subprocess.Popen([str(self._verified[0]), 'app-server', '--listen', 'stdio://'],
                                       **self._options())
            incoming, failed, reader = self._reader(process, bounded=False)
            self._write(process, {'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'codex_safe_resume_queue', 'version': '0.5.0'},
                'capabilities': {'experimentalApi': True}}}, deadline, writers)
            initialized = _response(incoming, failed, 1, deadline)
            if not isinstance(initialized.get('userAgent'), str) or not initialized['userAgent']:
                raise ValueError('invalid_initialize')
            self._write(process, {'method': 'initialized', 'params': {}}, deadline, writers)
            if before_send is not None and before_send() is not True:
                return outcome
            if time.monotonic() >= deadline:
                return outcome
            dispatched = True
            self._write(process, {'id': 2, 'method': 'thread/queue/add', 'params': params}, deadline, writers)
            reply = _response(incoming, failed, 2, deadline)
            submission = reply.get('queuedSubmission')
            if not isinstance(submission, dict):
                raise ValueError('invalid_receipt')
            receipt = _uuid(submission.get('id'))
            if (('threadId' in submission and submission['threadId'] != thread_id)
                    or ('clientUserMessageId' in submission
                        and submission['clientUserMessageId'] != attempt_id)):
                raise ValueError('wrong_receipt')
            self._owned.add((thread_id, receipt))
            outcome = {'outcome': 'accepted', 'queue_id': receipt}
        except Exception:
            outcome = {'outcome': 'unknown' if dispatched else 'not_started'}
        finally:
            if not self._finish(process, reader, writers) and dispatched:
                outcome = {'outcome': 'unknown'}
        return outcome

    def _finish(self, process, reader, writers=()):
        clean = True
        try:
            if any(writer.is_alive() for writer in writers) and process is not None:
                process.terminate()
            clean = _close(process, reader)
        except Exception:
            clean = False
        for writer in writers:
            writer.join(timeout=1)
            clean = clean and not writer.is_alive()
        return clean

    def _write(self, process, message, deadline, writers):
        failed = threading.Event()

        def write():
            try:
                self._send(process.stdin, message)
            except Exception:
                failed.set()
        writer = threading.Thread(target=write, daemon=True, name='queue-cli-writer')
        writers.append(writer)
        writer.start()
        writer.join(timeout=max(0, deadline - time.monotonic()))
        if writer.is_alive() or failed.is_set():
            raise ValueError('write_unavailable')

    def _send(self, stream, message):
        if message.get('method') not in ('initialize', 'initialized', 'account/read',
                                         'account/rateLimits/read', 'thread/queue/list',
                                         'thread/queue/delete', 'thread/queue/add'):
            raise ValueError('unapproved_method')
        pending = memoryview((json.dumps(message) + '\n').encode('utf-8'))
        while pending:
            count = stream.write(pending)
            if type(count) is not int or not 0 < count <= len(pending):
                raise ValueError('write_failed')
            pending = pending[count:]
        stream.flush()
