#!/usr/bin/env python3
"""Exercise the real calibration client/HTTP routes against a fake serial device."""
from collections import deque
import http.client
from http.server import HTTPServer
import json
from pathlib import Path
import sys
import subprocess
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pi.calibrate import Device, make_handler, validate


class Link:
    def __init__(self):
        self.replies = deque()
        self.commands = []
        self.packets = []
        self.cal = {1: [120, 160, 0, 0, 0, 0], 2: [120, 160, 0, 1, 0, 0]}
        self.version = 'alia-eye-wifi-7'
        self.fail = False
        self.swap_cal_ids = False
        self.closed = False
        self.authenticated = 0
        self.sync_gate = None
        self.sync_waiting = threading.Event()
        self.bad_sync = False

    def authenticate(self, token):
        assert token == 'a' * 64
        self.authenticated += 1

    def write(self, packet):
        self.packets.append(packet)
        for text in packet.decode().splitlines():
            self.command(text)
        return len(packet)

    def command(self, text):
        self.commands.append(text)
        parts = text.split()
        if text == 'STATUS':
            reply = f'STATUS {self.version} 00:11:22:33:44:55 1 0 0'
        elif text == 'INFO2':
            reply = 'INFO2 240 320 -285 285 -206 206'
        elif text == 'SAVECAL':
            for row in self.cal.values(): row[-1] = 0
            reply = 'OKSAVE'
        elif text == 'SYNC':
            reply = 'OKSYNC extra' if self.bad_sync else 'OKSYNC'
        elif parts[0] == 'EYE':
            number = int(parts[1])
            if parts[2] == 'XY':
                assert parts[3:] == ['0', '0']
                reply = f'OKXY {number} 0 0'
            elif parts[2] == 'RESETCAL':
                self.cal[number] = [120, 160, 0, number - 1, 0, 1]
                reply = f'OKCAL {number}'
            elif len(parts) == 3:
                reply = 'CAL ' + str(3-number if self.swap_cal_ids else number) + ' ' + ' '.join(map(str, self.cal[number]))
            else:
                if self.fail: raise TimeoutError('Simulated write failure')
                self.cal[number] = list(map(int, parts[3:])) + [1]
                reply = f'OKCAL {number}'
        else:
            raise AssertionError(text)
        self.replies.append((reply + '\n').encode())

    def read_until(self, *_):
        if self.replies and self.replies[0].startswith(b'OKSYNC') and self.sync_gate:
            self.sync_waiting.set()
            if not self.sync_gate.wait(.01):
                return b''
        return self.replies.popleft() if self.replies else b''

    def close(self):
        self.closed = True


link = Link()
device = object.__new__(Device)
device.serial = link
device.host = device.token = None
device.tcp = device.info = None
device.last_use = 0
base = dict(screen=1, center_x=134, center_y=172, angle=-7.3, top='opposite')
server = HTTPServer(('127.0.0.1', 0), make_handler(device))
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()


def request(path, data=None, *, headers=None, raw=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    body = None if data is None else json.dumps(data)
    if raw is not None: body = raw
    all_headers = {'Content-Type': 'application/json', 'X-Eye-Control': '1'}
    all_headers.update(headers or {})
    try:
        connection.request('GET' if body is None else 'POST', path, body=body, headers=all_headers)
        response = connection.getresponse()
        content = response.read()
        return response.status, content
    finally:
        connection.close()


try:
    for bad in ({}, dict(base, screen=True), dict(base, screen=3), dict(base, center_x=240),
                dict(base, center_y=-1), dict(base, center_x=12.5), dict(base, angle=True),
                dict(base, angle=float('inf')), dict(base, angle=float('nan')), dict(base, angle=181), dict(base, angle=10**500),
                dict(base, top='left'), dict(base, inverted=False), dict(base, extra='ignored')):
        count = len(link.commands)
        assert request('/api/calibration', bad)[0] == 400, bad
        assert len(link.commands) == count, 'Invalid input must not send even a center command'
    assert validate(dict(base, center_x=0, center_y=319, angle=180))[1] == (0, 319, 1800, 1, 0)
    assert request('/api/state')[0] == 200
    link.swap_cal_ids = True
    assert request('/api/state')[0] == 502, 'Batch replies must still match each physical screen ID'
    link.swap_cal_ids = False
    before_packets = len(link.packets)
    code, content = request('/api/calibration', base)
    result = json.loads(content)
    assert code == 200 and result['eyes'][0]['center_x'] == 134
    assert result['eyes'][0]['angle'] == -7.3 and result['eyes'][0]['unsaved']
    assert link.packets[before_packets:] == [b'STATUS\nINFO2\n',
        b'EYE 1 XY 0 0\nEYE 1 CAL 134 172 -73 1 0\nEYE 1 CAL\nEYE 2 CAL\n'], 'Two writes only; settings and readback must share a batch'
    assert link.cal[2] == [120, 160, 0, 1, 0, 0], 'Calibration must not change the other eye'
    assert request('/api/save', {})[0] == 200 and not any(row[-1] for row in link.cal.values())
    assert request('/api/reset', {'screen': 2})[0] == 200 and link.cal[1][:5] == [134, 172, -73, 1, 0]
    link.version = 'alia-eye-wifi-8'
    assert request('/api/state')[0] == 200
    link.version = 'alia-eye-wifi-9'
    link.sync_gate = threading.Event()
    pending_result = []
    before_packets = len(link.packets)
    pending_thread = threading.Thread(target=lambda: pending_result.append(request('/api/calibration', base)))
    pending_thread.start()
    try:
        assert link.sync_waiting.wait(1)
        assert pending_thread.is_alive() and not pending_result, 'Do not return HTTP success before the frame completes'
    finally:
        link.sync_gate.set()
        pending_thread.join(timeout=2)
        link.sync_gate = None
    assert pending_result[0][0] == 200
    assert link.packets[before_packets:] == [b'STATUS\nINFO2\n',
        b'EYE 1 XY 0 0\nEYE 1 CAL 134 172 -73 1 0\nEYE 1 CAL\nEYE 2 CAL\nSYNC\n']
    link.bad_sync = True
    assert request('/api/calibration', base)[0] == 502
    link.bad_sync = False
    link.version = 'alia-eye-wifi-7'
    assert request('/api/reset', {'screen': True})[0] == 400
    assert request('/api/save', {'unexpected': 1})[0] == 400
    for old in ('alia-eye-wifi-5', 'alia-eye-wifi-6'):
        link.version = old
        before = len(link.commands)
        assert request('/api/calibration', base)[0] == 502
        assert link.commands[before:] == ['STATUS', 'INFO2'], 'Old firmware must be rejected before mutation'
    link.version = 'alia-eye-wifi-7'
    link.fail = True
    before = len(link.commands)
    code, content = request('/api/calibration', base)
    assert code == 502 and '尚未确认' in content.decode()
    assert link.commands[before:].count('EYE 1 CAL 134 172 -73 1 0') == 1, 'Never replay a timed-out calibration'
    link.fail = False
    for headers in ({'Origin': 'https://example.com'}, {'Host': 'example.com'}, {'X-Eye-Control': ''}):
        before = len(link.commands)
        assert request('/api/calibration', base, headers=headers)[0] == 403
        assert len(link.commands) == before
    for raw in ('{', '{"screen": NaN}', 'x' * 2049):
        assert request('/api/calibration', raw=raw)[0] == 400
    assert request('/api/save', {}, headers={'Content-Type': 'text/plain'})[0] == 400
    assert request('/not-found')[0] == 404 and request('/api/unknown', {})[0] == 404
    code, html = request('/')
    assert code == 200 and '眼睛中心'.encode() in html and b'canvas' in html
    assert b'inverted' not in html and '反色校正'.encode() not in html
    assert request('/eye.png')[1] == (ROOT / 'assets/eye-texture.png').read_bytes()
    assert request('/../firmware/src/main.cpp')[0] == 404
    duplicate = subprocess.run([sys.executable, str(ROOT/'pi/calibrate.py'), '--port', '/nonexistent-calibration-test',
                                '--web-port', str(server.server_port)], capture_output=True, text=True, timeout=5)
    assert duplicate.returncode == 1 and 'Address already in use' in duplicate.stderr
    assert 'could not open port' not in duplicate.stderr, 'A duplicate launcher must not touch USB'
    print('PASS calibration HTTP/client: centers/angles/orientation/colors, per-eye isolation, save/reset, old firmware guard, invalid input, origin/host guard, no retry, static assets')
finally:
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()

# A drag reuses only its authenticated, verified connection; expiry/errors discard it.
links, clock = [], [10.0]
def connect(host):
    assert host == 'test-esp32'
    link = Link()
    link.version = 'alia-eye-wifi-9'
    links.append(link)
    return link

with patch('pi.calibrate.TcpConnection', connect), patch('pi.calibrate.read_token', return_value='a'*64), \
     patch('pi.calibrate.time.monotonic', side_effect=lambda: clock[0]):
    device = Device(host='test-esp32', token_file='private-token')
    try:
        device.execute('/api/calibration', base)
        clock[0] += .1
        device.execute('/api/calibration', dict(base, center_x=135))
        assert len(links) == 1 and links[0].authenticated == 1
        assert links[0].commands.count('STATUS') == links[0].commands.count('INFO2') == 1
        assert links[0].packets[-1] == b'EYE 1 XY 0 0\nEYE 1 CAL 135 172 -73 1 0\nEYE 1 CAL\nEYE 2 CAL\nSYNC\n'
        assert links[0].commands.count('SAVECAL') == 0
        clock[0] += .8
        device.expire_connection()
        assert links[0].closed and device.tcp is None and device.info is None
        device.execute('/api/calibration', base)
        assert len(links) == 2 and links[1].packets[0] == b'STATUS\nINFO2\n'
        links[1].fail = True
        before = len(links[1].commands)
        try:
            device.execute('/api/calibration', base)
            raise AssertionError('Expected a timeout')
        except TimeoutError:
            pass
        assert links[1].commands[before:].count('EYE 1 CAL 134 172 -73 1 0') == 1
        assert links[1].closed and device.tcp is None and device.info is None
        device.execute('/api/state')
        assert len(links) == 3 and links[2].commands[:2] == ['STATUS', 'INFO2']
    finally:
        device.close()
    assert links[-1].closed
print('PASS calibration burst TCP: authenticate/verify once, bounded idle release, fresh reconnect, failed writes never replayed')
