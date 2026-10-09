#!/usr/bin/env python3
"""Run the actual renderer and Raspberry Pi serial/TCP client checks without a board."""
import os
from pathlib import Path
import pty
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image
import serial

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from pi.eye_control import EyeController, TcpConnection, read_token
from tools.prepare_image import rgb565


def rejects(error, action):
    try:
        action()
    except error:
        return
    raise AssertionError(f'Expected {error.__name__}')


(ROOT / '.build').mkdir(exist_ok=True)
subprocess.run(['c++', '-std=c++11', '-Wall', '-Wextra', '-Werror', '-Ifirmware/include',
                'tests/test_core.cpp', '-o', '.build/test_core'], check=True)
subprocess.run(['.build/test_core'], check=True)
colors = Image.new('RGB', (4, 1))
colors.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])
assert rgb565(colors) == bytes.fromhex('00f8 e007 1f00 ffff')
with Image.open('assets/eye-texture.png') as texture:
    with Image.open('assets/eye-source.png') as source:
        upright = source.convert('RGB').crop((512, 120, 1024, 896))
        upright.thumbnail((492, 325), Image.Resampling.LANCZOS)
    expected = upright.transpose(Image.Transpose.ROTATE_270)
    assert texture.size == (325, 214) and texture.tobytes() == expected.tobytes(), 'Texture must keep clockwise 90-degree orientation at the reduced size'
    assert rgb565(texture) == Path('firmware/data/eye.rgb565').read_bytes()
for name in ('center', 'up', 'down', 'left', 'right'):
    with Image.open(f'.build/{name}.ppm') as frame:
        frame.save(f'.build/{name}.png')
print('PASS clockwise 90-degree orientation, 325x214 size, RGB565 byte order and embedded asset consistency')

master, slave = pty.openpty()
stop = threading.Event()
commands = []


def device():
    pending = b''
    while not stop.is_set():
        if not select.select([master], [], [], 0.1)[0]:
            continue
        pending += os.read(master, 128)
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            commands.append(line)
            if line == b'INFO':
                os.write(master, b'boot log\nINFO 240 320 -189 189\n')
            elif line.startswith(b'Y '):
                y = min(189, max(-189, int(line[2:])))
                os.write(master, f'OK {y}\n'.encode())
            elif line != b'SILENT':
                os.write(master, b'ERR command\n')


thread = threading.Thread(target=device)
try:
    with serial.Serial(os.ttyname(slave), 115200, timeout=0.05, write_timeout=1) as connection:
        thread.start()
        eye = EyeController(connection)
        assert eye.connect() == (240, 320, -189, 189)
        for requested, actual in [(-60, -60), (0, 0), (60, 60), (-1000, -189), (1000, 189)]:
            assert eye.set_y(requested) == actual
        for invalid in (1.5, True, 2**31):
            rejects(ValueError, lambda: eye.set_y(invalid))
        rejects(RuntimeError, lambda: eye.request('BAD', 'OK'))
        rejects(TimeoutError, lambda: eye.request('SILENT', 'OK', timeout=0.15))
        assert eye.set_y(0) == 0
    result = subprocess.run([sys.executable, 'pi/eye_control.py', '--port', os.ttyname(slave), '--y', '-60'],
                            check=True, capture_output=True, text=True)
    assert 'OK y=-60 px' in result.stdout
    assert b'Y -60' in commands and b'Y 1000' in commands
finally:
    stop.set()
    if thread.is_alive():
        thread.join(timeout=1)
    os.close(master)
    os.close(slave)
print('PASS real pySerial over PTY: handshake, commands, clamping, errors, timeout, recovery, CLI')

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
listener.settimeout(0.1)
tcp_port = listener.getsockname()[1]
stop.clear()
token = '1' * 64  # Fake test credential, never used on a real device.
tcp_commands = []
pause_y_reply = False
reply_pending = threading.Event()
release_reply = threading.Event()


def tcp_device():
    while not stop.is_set():
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection, connection.makefile('rb') as stream:
            authentication = stream.readline(128).rstrip(b'\n')
            if authentication != ('AUTH ' + token).encode():
                connection.sendall(b'ERR auth\n')
                continue
            connection.sendall(b'OK AUTH\n')
            while line := stream.readline(128).rstrip(b'\n'):
                tcp_commands.append(line)
                if line == b'INFO':
                    connection.sendall(b'INFO 240 ')
                    time.sleep(0.15)
                    connection.sendall(b'320 -189 189\n')
                elif line.startswith(b'Y '):
                    y = min(189, max(-189, int(line[2:])))
                    if pause_y_reply and y != 0 and not reply_pending.is_set():
                        reply_pending.set()
                        release_reply.wait(timeout=3)
                    connection.sendall(f'OK {y}\n'.encode())
                elif line == b'CLOSE':
                    break
                elif line == b'LONG':
                    connection.sendall(b'x' * 129 + b'\n')
                    break
                elif line != b'SILENT':
                    connection.sendall(b'ERR command\n')


tcp_thread = threading.Thread(target=tcp_device)
try:
    tcp_thread.start()
    with tempfile.TemporaryDirectory(dir=ROOT / '.build') as directory:
        token_path = Path(directory) / 'token'
        token_path.write_text(token + '\n', encoding='ascii')
        token_path.chmod(0o600)
        assert read_token(token_path) == token
        token_path.chmod(0o644)
        rejects(ValueError, lambda: read_token(token_path))
        token_path.chmod(0o600)
        token_path.write_text(token + '\n\nextra', encoding='ascii')
        rejects(ValueError, lambda: read_token(token_path))
        token_path.write_text(token + '\n', encoding='ascii')
        with TcpConnection('127.0.0.1', tcp_port) as connection:
            connection.authenticate(read_token(token_path))
            eye = EyeController(connection)
            assert eye.connect() == (240, 320, -189, 189)
            for requested, actual in [(-60, -60), (0, 0), (60, 60), (-1000, -189), (1000, 189)]:
                assert eye.set_y(requested) == actual
            rejects(RuntimeError, lambda: eye.request('BAD', 'OK'))
            rejects(TimeoutError, lambda: eye.request('SILENT', 'OK', timeout=0.15))
            assert eye.set_y(0) == 0
            rejects(OSError, lambda: eye.request('CLOSE', 'OK'))
        with TcpConnection('127.0.0.1', tcp_port) as connection:
            rejects(RuntimeError, lambda: connection.authenticate('0' * 64))
        with TcpConnection('127.0.0.1', tcp_port) as connection:
            connection.authenticate(token)
            rejects(OSError, lambda: EyeController(connection).request('LONG', 'OK'))
        cli = [sys.executable, 'pi/eye_control.py', '--host', '127.0.0.1', '--tcp-port', str(tcp_port),
               '--token-file', str(token_path), '--y', '-60']
        result = subprocess.run(cli, check=True, capture_output=True, text=True)
        assert 'OK y=-60 px' in result.stdout
        assert token not in result.stdout + result.stderr
        demo_cli = cli[:-2] + ['--demo', '--seconds']
        result = subprocess.run(demo_cli + ['0.15'], check=True, capture_output=True, text=True)
        assert '已发送居中指令' in result.stdout and tcp_commands[-1] == b'Y 0'
        pause_y_reply = True
        process = subprocess.Popen(demo_cli + ['5'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert reply_pending.wait(timeout=3), 'Demo did not send a nonzero move'
            process.send_signal(signal.SIGINT)
            time.sleep(0.1)  # Deliver SIGINT while the nonzero ACK is still withheld.
            release_reply.set()
            stdout, stderr = process.communicate(timeout=4)
            assert process.returncode == 0, (stdout, stderr)
            assert '已发送居中指令' in stdout and tcp_commands[-1] == b'Y 0'
        finally:
            release_reply.set()
            if process.poll() is None:
                process.kill()
                process.communicate()
        token_path.write_text('0' * 64, encoding='ascii')
        result = subprocess.run(cli, capture_output=True, text=True)
        assert result.returncode == 1 and '认证失败' in result.stderr
        assert '0' * 64 not in result.stdout + result.stderr
        assert b'Y -60' in tcp_commands and b'Y 1000' in tcp_commands
finally:
    stop.set()
    tcp_thread.join(timeout=1)
    listener.close()
print('PASS real TCP sockets: authentication, protected token file, split replies, bounds, timeout, disconnect, CLI, timed demo and SIGINT during pending ACK')
print('ALL CHECKS PASSED (host simulation; hardware not connected)')
