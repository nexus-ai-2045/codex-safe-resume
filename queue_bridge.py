from __future__ import annotations

import os
from pathlib import Path

from connection_settings import _fingerprint
from core import check_id, observe_resume_quota
from desktop_bridge import BridgeError
from desktop_ipc import DesktopIpc
from queue_history import QueueHistory
import queue_platform
from queue_transport import QueueTransport


_ERROR = '対象チャットと安全なキュー接続を確認できません。'


class QueueNotStarted(BridgeError):
    pass


def _home(value=None):
    return Path(value if value is not None else os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser().absolute()


class QueueBridge:
    def __init__(self, config=None, home=None, timeout=15, for_pending=False):
        self.config = dict(config or {})
        self.home = _home(home)
        self.timeout = timeout
        self.for_pending = for_pending is True
        self.evidence = None
        self.transport = None
        self.ipc = None
        self.connected = False
        self.history = QueueHistory(self.home)
        self.attempted_failures = set()
        self._cli = None

    def __enter__(self):
        try:
            self.evidence = queue_platform.desktop_for_home(self.home)
            self.transport = QueueTransport(self.evidence['executable'], self.home, timeout=self.timeout)
            if not self.transport.check():
                raise ValueError('cli')
            self._cli = _fingerprint(self.evidence['executable'])
            self.ipc = DesktopIpc(self.evidence, timeout=min(self.timeout, 30))
            self.ipc.__enter__()
            self.connected = True
            return self
        except Exception:
            self._close()
            if self.for_pending:
                try:
                    self._pending_connection()
                    return self
                except Exception:
                    pass
            raise BridgeError(_ERROR) from None

    def _configured_home(self):
        value = self.config.get('codex_home')
        if not isinstance(value, str) or not value or '\0' in value:
            raise ValueError('home')
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            raise ValueError('home')
        return path

    def _pending_connection(self):
        if self.config.get('mode') != 'queue' or self._configured_home() != self.home:
            raise ValueError('configuration')
        queue_platform._home(self.home)
        path, digest = _fingerprint(self.config.get('executable', ''))
        if digest != self.config.get('sha256'):
            raise ValueError('cli_changed')
        self.transport = QueueTransport(path, self.home, timeout=self.timeout)
        if not self.transport.check() or _fingerprint(path) != (path, digest):
            raise ValueError('cli')
        self._cli = path, digest

    def __exit__(self, *args):
        self._close()

    def _close(self):
        try:
            if self.ipc is not None:
                self.ipc.__exit__(None, None, None)
        except Exception:
            pass
        finally:
            self.ipc = None
            self.connected = False
            self.evidence = None

    def _selected(self):
        try:
            return (self._cli is not None and self.config.get('mode') == 'queue'
                    and self.config.get('desktop_confirmed') is True
                    and self.config.get('manual_exclusion_acknowledged') is True
                    and self._configured_home() == self.home
                    and Path(self.config.get('executable', '')) == self._cli[0]
                    and self.config.get('sha256') == self._cli[1]
                    and _fingerprint(self._cli[0]) == self._cli)
        except Exception:
            return False

    def capabilities(self):
        try:
            live = bool(self.connected and self.ipc is not None and self.evidence is not None
                        and queue_platform.revalidate(self.evidence))
        except Exception:
            live = False
        guarded = live and not self.for_pending and self._selected()
        return {'safe_admission': False, 'guarded_admission': guarded, 'desktop_binding': live,
                'binding_source': 'native_desktop_ipc' if live else 'unavailable',
                'reason': 'guarded' if guarded else 'connection_setup_required'}

    def prepare_thread(self, thread_id):
        try:
            thread = check_id(thread_id)
            if not self.connected or not queue_platform.loaded(thread, self.evidence):
                raise ValueError('not_loaded')
        except Exception:
            raise BridgeError(_ERROR) from None

    def snapshot(self, thread_id):
        try:
            thread = check_id(thread_id)
            self.prepare_thread(thread)
            value = self.ipc.snapshot(thread)
            candidate = self.history.candidate(thread)
            if (value.get('id') != thread or value.get('observation_verified') is not True
                    or candidate.get('projection_current') is not True
                    or candidate.get('latest_turn') != value.get('latest_turn')
                    or candidate.get('status') != value.get('turn_status')
                    or candidate.get('failure') is not value.get('limited')
                    or not queue_platform.revalidate(self.evidence)):
                raise ValueError('snapshot')
            return value
        except Exception:
            raise BridgeError(_ERROR) from None

    def quota(self):
        try:
            return self.transport.rpc('account/rateLimits/read', {})
        except Exception:
            raise BridgeError(_ERROR) from None

    def _admit(self, thread, failure):
        if self.capabilities().get('guarded_admission') is not True:
            return False
        allowed, _ = observe_resume_quota(self.quota())
        if allowed is not True:
            return False
        current = self.snapshot(thread)
        return (current.get('latest_turn') == failure and current.get('failure_turn') == failure
                and current.get('limited') is True and current.get('idle') is True
                and current.get('awaiting') is False and not self.history.pending(thread))

    def send_once(self, thread_id, expected_turn, command='続けて', *, attempt_id=None, before_send=None):
        try:
            thread = check_id(thread_id)
            failure = check_id(expected_turn)
            attempt = check_id(attempt_id)
            if command != '続けて' or self.for_pending or (thread, failure) in self.attempted_failures:
                raise ValueError('not_admitted')
        except Exception:
            raise BridgeError(_ERROR) from None

        try:
            if not self._admit(thread, failure):
                raise ValueError('not_admitted')
        except Exception:
            raise QueueNotStarted(_ERROR) from None

        def guard():
            try:
                return self._admit(thread, failure) and (before_send is None or before_send() is True)
            except Exception:
                return False

        self.attempted_failures.add((thread, failure))
        try:
            result = self.transport.enqueue(thread, command, attempt, before_send=guard)
            outcome = result.get('outcome')
            if outcome == 'accepted':
                return {'delivery': 'queued', 'queue': {'id': check_id(result.get('queue_id'))}}
            return {'delivery': 'not_started' if outcome == 'not_started' else 'unknown'}
        except Exception:
            return {'delivery': 'unknown'}

    def receipt(self, thread_id, attempt_id, queue_id=None):
        try:
            thread, attempt = check_id(thread_id), check_id(attempt_id)
            if queue_id is not None:
                queue_id = check_id(queue_id)
            return self.history.receipt(thread, attempt, queue_id=queue_id, client_id=attempt)
        except Exception:
            return {'delivery': 'unknown'}

    def _own_pending(self, thread, attempt):
        plain = [{'type': 'text', 'text': '続けて'}]
        serialized = [{'type': 'text', 'text': '続けて', 'text_elements': []}]
        return [row for row in self.history.pending(thread)
                if row.get('client_id') == attempt and row.get('input') in (plain, serialized)]

    def find_pending(self, thread_id, attempt_id):
        try:
            thread, attempt = check_id(thread_id), check_id(attempt_id)
            rows = self._own_pending(thread, attempt)
            if len(rows) > 1:
                raise ValueError('ambiguous')
            return check_id(rows[0].get('queue_id')) if rows else None
        except Exception:
            raise BridgeError(_ERROR) from None

    def cancel_pending(self, thread_id, attempt_id, queue_id):
        try:
            thread, attempt, receipt = check_id(thread_id), check_id(attempt_id), check_id(queue_id)
            rows = self._own_pending(thread, attempt)
            if len(rows) != 1 or rows[0].get('queue_id') != receipt:
                return {'delivery': 'unknown', 'deleted': False}
            if self._cli is None or _fingerprint(self._cli[0]) != self._cli:
                raise ValueError('cli_changed')
            self.transport._owned.add((thread, receipt))
            result = self.transport.rpc('thread/queue/delete', {'threadId': thread, 'queuedSubmissionId': receipt})
            return {'delivery': 'unknown', 'deleted': result.get('deleted') is True}
        except Exception:
            return {'delivery': 'unknown', 'deleted': False}


def prepare_config(home=None):
    with QueueBridge(home=home, for_pending=True) as bridge:
        if not bridge.connected:
            raise BridgeError(_ERROR)
        path, digest = bridge._cli
        return {'mode': 'queue', 'executable': str(path), 'sha256': digest,
                'codex_home': str(bridge.home), 'desktop_confirmed': False,
                'manual_exclusion_acknowledged': False}
