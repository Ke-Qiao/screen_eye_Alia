#!/usr/bin/env python3
"""Wi-Fi provisioning checks with private files and a fake ESP32 on a real PTY."""
from collections import deque
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.provision_wifi import config_packet, load_credentials, load_token, provision, wait_reply

MAC = 'aa:bb:cc:dd:ee:ff'
TOKEN = 'a7' * 32
PASSWORD = 'secret password'
STATUS = f'STATUS alia-eye-wifi-2 {MAC} 0 0 0\n'.encode()


def rejects(error, action):
    try:
        action()
    except error as caught:
        assert TOKEN not in str(caught) and PASSWORD not in str(caught), 'Error leaked credentials'
        return
    raise AssertionError(f'Expected {error.__name__}')


class Link:
    def __init__(self, *frames):
        self.frames = deque(frames)
        self.sent = []

    def write(self, packet):
        self.sent.append(packet)
        return len(packet)

    def read_until(self, separator, limit):
        if self.frames:
            return self.frames.popleft()
        time.sleep(0.001)
        return b''


packet = config_packet('蓝色眼睛', PASSWORD, TOKEN)
fields = packet.decode().split()
assert bytes.fromhex(fields[1]).decode() == '蓝色眼睛'
assert bytes.fromhex(fields[2]).decode() == PASSWORD
assert fields[3] == TOKEN and packet.endswith(b'\n')
assert len(config_packet('x' * 32, 'F' * 64, TOKEN)) == 266
for ssid, password in [('', PASSWORD), ('界' * 11, PASSWORD), ('x\0y', PASSWORD),
                       ('x', 'short'), ('x', '界' * 22), ('x', 'z' * 64), ('x', 'pass\0word')]:
    rejects(ValueError, lambda: config_packet(ssid, password, TOKEN))
rejects(ValueError, lambda: config_packet('x', PASSWORD, TOKEN.upper()))

link = Link(b'boot log\n', STATUS[:8], STATUS[8:], b'OK CONFIG\n', b'NET 192.168.10.99 4242\n')
assert provision(link, 'blue', PASSWORD, TOKEN, MAC.upper()) == (MAC, 0, '192.168.10.99', 4242)
assert link.sent == [b'STATUS\n', config_packet('blue', PASSWORD, TOKEN)]
for version in (f'alia-eye-wifi-{number}'.encode() for number in range(3, 11)):
    current = Link(STATUS.replace(b'alia-eye-wifi-2', version), b'OK CONFIG\n', b'NET 192.168.10.99 4242\n')
    assert provision(current, 'blue', PASSWORD, TOKEN, MAC) == (MAC, 0, '192.168.10.99', 4242)
wrong = Link(STATUS)
rejects(RuntimeError, lambda: provision(wrong, 'blue', PASSWORD, TOKEN, '00:00:00:00:00:00'))
assert wrong.sent == [b'STATUS\n'], 'Must not configure a mismatched device'
for status in [STATUS.replace(b' 0 0 0', b' 2 0 0'), STATUS.replace(b' 0 0 0', b' 0 2147483648 0'),
               STATUS.replace(b'alia-eye-wifi-2', b'other-firmware')]:
    invalid = Link(status)
    rejects(RuntimeError, lambda: provision(invalid, 'blue', PASSWORD, TOKEN))
    assert invalid.sent == [b'STATUS\n']
for net in [b'NET 0.0.0.0 4242\n', b'NET 192.168.10.99 23\n', b'NET invalid 4242\n']:
    rejects(RuntimeError, lambda: provision(Link(STATUS, b'OK CONFIG\n', net), 'blue', PASSWORD, TOKEN))
rejects(RuntimeError, lambda: wait_reply(Link(f'ERR {TOKEN} {PASSWORD}\n'.encode()), 'OK', bytearray(), 0.1))
rejects(RuntimeError, lambda: wait_reply(Link(b'x' * 257), 'OK', bytearray(), 0.1))
rejects(TimeoutError, lambda: wait_reply(Link(), 'NET', bytearray(), 0.01))

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    wifi = root / 'wifi.json'
    wifi.write_text(json.dumps({'ssid': 'blue', 'password': PASSWORD}))
    wifi.chmod(0o600)
    assert load_credentials(wifi) == ('blue', PASSWORD)
    token = root / 'token'
    original = load_token(token)
    assert len(original) == 64 and token.stat().st_mode & 0o777 == 0o600
    assert load_token(token) == original, 'Existing valid token must not be replaced'
    token.write_text(TOKEN + '\n')
    token.chmod(0o644)
    rejects(ValueError, lambda: load_token(token))
    assert token.read_text().strip() == TOKEN
    token.chmod(0o600)
    symlink = root / 'link'
    symlink.symlink_to(token)
    rejects(OSError, lambda: load_token(symlink))
    wifi.chmod(0o644)
    rejects(ValueError, lambda: load_credentials(wifi))
    wifi.chmod(0o600)
    wifi.write_text('{"password": "secret password", BROKEN')
    rejects(ValueError, lambda: load_credentials(wifi))
    wifi.write_text(json.dumps({'ssid': 'blue', 'password': PASSWORD}))

    master, slave = pty.openpty()
    stop = threading.Event()
    commands = []
    errors = []

    def device():
        pending = b''
        try:
            while not stop.is_set():
                if not select.select([master], [], [], 0.05)[0]:
                    continue
                pending += os.read(master, 128)
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    commands.append(line)
                    if line == b'STATUS':
                        os.write(master, STATUS[:10])
                        time.sleep(0.15)  # Force a pySerial timeout between parts of one line.
                        os.write(master, STATUS[10:])
                    elif line.startswith(b'CONFIG '):
                        assert line + b'\n' == config_packet('blue', PASSWORD, TOKEN)
                        os.write(master, b'OK CONFIG\nNET 192.168.')
                        time.sleep(0.15)
                        os.write(master, b'10.99 4242\n')
                    else:
                        os.write(master, b'ERR command\n')
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=device)
    try:
        thread.start()
        result = subprocess.run([sys.executable, str(ROOT / 'tools/provision_wifi.py'),
                                 '--port', os.ttyname(slave), '--wifi-file', str(wifi),
                                 '--token-file', str(token), '--expect-mac', MAC],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert 'NET 192.168.10.99 4242' in result.stdout
        assert PASSWORD not in result.stdout + result.stderr and TOKEN not in result.stdout + result.stderr
        assert any(line.startswith(b'CONFIG ') for line in commands)
    finally:
        stop.set()
        thread.join(timeout=1)
        os.close(master)
        os.close(slave)
    assert not thread.is_alive() and not errors

print('PASS Wi-Fi provisioning: private files, credential validation, identity check, split serial replies, CLI, no secret logs (host simulation)')
