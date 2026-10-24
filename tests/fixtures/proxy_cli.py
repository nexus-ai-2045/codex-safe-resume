"""隔離テスト用CLI。公式CLI・モデル・認証・ユーザー会話へ接続しない。"""
import base64
import hashlib
import json
import os
import sys

if sys.argv[1:] == ['app-server', 'daemon', 'version']:
    print(json.dumps({'cliVersion': '0.163.0', 'appServerVersion':
                     'different' if os.environ.get('FIXTURE_MODE') == 'version-mismatch' else '0.163.0'}))
    raise SystemExit(0)
if sys.argv[1:] != ['app-server', 'proxy']:
    raise SystemExit(2)

import websocket
from websocket import ABNF

headers = {}
while True:
    line = sys.stdin.buffer.readline(8192)
    if not line: raise SystemExit(3)
    if line == b'\r\n': break
    if b':' in line:
        key, value = line.split(b':', 1)
        headers[key.strip().lower()] = value.strip()
key = headers[b'sec-websocket-key']
accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
sys.stdout.buffer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
sys.stdout.buffer.flush()


class Pipes:
    def recv(self, count): return os.read(sys.stdin.fileno(), count)
    def gettimeout(self): return None
    def send(self, data): return os.write(sys.stdout.fileno(), data)


def send(value, opcode=ABNF.OPCODE_TEXT):
    frame = ABNF.create_frame(json.dumps(value) if opcode == ABNF.OPCODE_TEXT else value, opcode)
    frame.mask_value = 0
    sys.stdout.buffer.write(frame.format())
    sys.stdout.buffer.flush()


client = websocket.WebSocket()
client.sock = Pipes()
client.connected = True
turn, status, state = 'failure', 'failed', 'idle'
thread_id = '123e4567-e89b-12d3-a456-426614174000'
while True:
    frame = client.recv_frame()
    if frame.opcode == ABNF.OPCODE_CLOSE:
        send(frame.data, opcode=ABNF.OPCODE_CLOSE)
        break
    message = json.loads(frame.data)
    method = message['method']
    if 'id' not in message: continue
    if method == 'initialize':
        result = {'userAgent': 'isolated-fixture/0.163.0'}
    elif method == 'account/rateLimits/read':
        result = {'rateLimits': {'primary': {'usedPercent': 0}, 'secondary': {'usedPercent': 0}}}
    elif method == 'thread/read':
        result = {'thread': {'id': thread_id, 'status': {'type': state}, 'turns': [
            {'id': turn, 'status': status, 'error': {'codexErrorInfo': 'usageLimitExceeded'} if status == 'failed' else None}]}}
        if os.environ.get('FIXTURE_MODE') == 'history-lag':
            result['thread']['turns'] = [{'id': 'failure', 'status': 'failed',
                                         'error': {'codexErrorInfo': 'usageLimitExceeded'}}]
    elif method == 'thread/turns/list':
        result = {'data': [{'id': turn, 'status': status,
                           'error': {'codexErrorInfo': 'usageLimitExceeded'} if status == 'failed' else None}]}
    elif method == 'turn/start':
        assert message['params']['threadId'] == thread_id
        assert message['params']['input'] == [{'type': 'text', 'text': '続けて'}]
        assert message['params']['clientUserMessageId']
        with open(os.environ['FIXTURE_WRITES'], 'a', encoding='utf-8') as output:
            output.write(json.dumps(message['params']) + '\n')
        if os.environ.get('FIXTURE_MODE') == 'lost-reply':
            raise SystemExit(0)
        turn, status, state = 'resumed', 'inProgress', 'active'
        result = {'turn': {'id': turn}}
    else:
        raise SystemExit(4)
    if method == 'thread/read' and os.environ.get('FIXTURE_MODE') == 'approval':
        send({'id': 99, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': thread_id}})
    send({'id': message['id'], 'result': result})
