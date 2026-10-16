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
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw, ImageFilter
import serial

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
from pi.eye_control import EyeController, TcpConnection, read_token
from tools.prepare_image import rgb565, panel_texture


def rejects(error, action):
    try:
        action()
    except error as raised:
        return raised
    raise AssertionError(f'Expected {error.__name__}')


(ROOT / '.build').mkdir(exist_ok=True)
subprocess.run(['c++', '-std=c++11', '-Wall', '-Wextra', '-Werror', '-Ifirmware/include',
                'tests/test_core.cpp', '-o', '.build/test_core'], check=True)
subprocess.run(['.build/test_core'], check=True)
subprocess.run(['c++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Itests/stubs', '-Ifirmware/include',
                'tests/test_panels.cpp', '-o', '.build/test_panels'], check=True)
subprocess.run(['.build/test_panels'], check=True)
colors = Image.new('RGB', (4, 1))
colors.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])
assert rgb565(colors) == bytes.fromhex('00f8 e007 1f00 ffff')
# A bright highlight enclosed by a dark iris must not become background.
sample = Image.new('RGB', (13, 13), 'white')
ImageDraw.Draw(sample).rectangle((1, 1, 11, 11), fill=(0, 15, 61))
sample.putpixel((6, 6), (255, 255, 255))
sample.putpixel((0, 6), (110, 113, 124))  # Gray antialiasing from the original white matte.
native = panel_texture(sample)
assert native[:26] == b'\0' * 26
assert native[168:170] == b'\xff\xff', 'Preserve enclosed bright highlights'
assert native[142:144] == rgb565(sample)[142:144], 'Preserve dark iris/pupil pixels'
assert native[156:158] == b'\0\0', 'Remove exterior matte completely'
assert int.from_bytes(native[158:160], 'little') < int.from_bytes(rgb565(sample)[158:160], 'little'), 'Feather the rim toward native white'
assert sample.getpixel((0, 0)) == (255, 255, 255), 'Do not modify the browser image'
with Image.open('assets/eye-texture.png') as texture:
    with Image.open('assets/eye-source.png') as source:
        upright = source.convert('RGB').crop((512, 120, 1024, 896))
        upright.thumbnail((344, 228), Image.Resampling.LANCZOS)
    expected = upright
    assert texture.size == (150, 228) and texture.tobytes() == expected.tobytes(), 'Texture must keep the original upright orientation at 70 percent of the previous portrait size'
    native = Path('firmware/data/eye.rgb565').read_bytes()
    original = rgb565(texture)
    assert panel_texture(texture) == native
    # Independent flood fill includes the antialiased boundary, but not enclosed highlights.
    exterior = Image.new('L', texture.size)
    colors = texture.convert('RGB').tobytes()
    pixels = list(zip(colors[0::3], colors[1::3], colors[2::3]))
    exterior.putdata([255 if min(pixel) >= 8 else 0 for pixel in pixels])
    for x in range(texture.width):
        for y in (0, texture.height - 1):
            if exterior.getpixel((x, y)) == 255: ImageDraw.floodfill(exterior, (x, y), 127)
    for y in range(texture.height):
        for x in (0, texture.width - 1):
            if exterior.getpixel((x, y)) == 255: ImageDraw.floodfill(exterior, (x, y), 127)
    background = exterior.tobytes()
    opaque = Image.frombytes('L', texture.size, bytes(0 if value == 127 else 255 for value in background))
    core = opaque.filter(ImageFilter.MinFilter(7)).tobytes()
    changed = matte = softened = protected = 0
    for i, value in enumerate(background):
        pixel, before = native[2*i:2*i+2], original[2*i:2*i+2]
        if value == 127:
            assert pixel == b'\0\0'
            matte += min(pixels[i]) < 240
            changed += 1
        elif core[i]:
            assert pixel == before, 'Every foreground pixel must retain its original RGB565 value'
            protected += 1
        else:
            actual_value, previous_value = int.from_bytes(pixel, 'little'), int.from_bytes(before, 'little')
            for shift, mask in ((11, 31), (5, 63), (0, 31)):
                assert ((actual_value >> shift) & mask) <= ((previous_value >> shift) & mask)
            softened += pixel != before
    assert 0 < changed < texture.width * texture.height
    assert any(value == 255 for value in background), 'Check the real artwork contains enclosed bright highlights'
    assert 0 < matte < 800 and 0 < softened < 2200 and protected > 20000, 'Limit treatment to the thin outer rim'
for name in ('center', 'up', 'down', 'left', 'right'):
    with Image.open(f'.build/{name}.ppm') as frame:
        frame.save(f'.build/{name}.png')
print(f'PASS upright 150x228 asset: {changed-matte} exterior and {matte} matte-edge pixels cleared; {softened} rim pixels feathered; {protected} interior pixels preserved')

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
            elif line == b'EYE 1 Y 48':
                for fragment in (b'WIFI_EVENT 123 UP\nO', b'K 1 ', b'48\r', b'\n'):
                    os.write(master, fragment)
                    time.sleep(0.12)  # Cross several real pySerial read_until timeouts.
            elif line == b'EYE 2 POS':
                os.write(master, b'POS 2 0 -')
                time.sleep(0.12)
                os.write(master, b'14 0 -14\n')
            elif line == b'BOUNDARY':
                os.write(master, b'OK ' + b'x' * 60)
                time.sleep(0.12)
                os.write(master, b'x' * 64 + b'\n')
            elif line == b'LONG':
                os.write(master, b'x' * 80)
                time.sleep(0.12)
                os.write(master, b'x' * 49 + b'\n')
            elif line == b'PARTIAL':
                os.write(master, b'OK ')
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
        previous_count = len(commands)
        started = time.monotonic()
        rejects(TimeoutError, lambda: eye.request('PARTIAL', 'OK', timeout=0.15))
        assert 0.15 <= time.monotonic() - started < 0.75, 'Partial line must not extend the request deadline'
        assert commands[previous_count:] == [b'PARTIAL'], 'Timed-out request must not be replayed'
        rejects(TimeoutError, lambda: eye.request('SILENT', 'OK', timeout=0.15))
        assert eye.set_y(0) == 0
        previous_count = len(commands)
        assert eye.set_y(48, screen=1) == 48
        assert commands[previous_count:] == [b'EYE 1 Y 48'], 'Split reply must not replay the move'
        assert eye.request_eye('POS', 'POS', 2) == ['POS', '0', '-14', '0', '-14']
        previous_count = len(commands)
        assert eye.request_many([('INFO', 'INFO'), ('EYE 2 POS', 'POS')]) == [
            ['INFO', '240', '320', '-189', '189'], ['POS', '2', '0', '-14', '0', '-14']]
        assert commands[previous_count:] == [b'INFO', b'EYE 2 POS'], 'A split batch reply must not replay either command'
        previous_count = len(commands)
        rejects(ValueError, lambda: eye.request_many([]))
        rejects(ValueError, lambda: eye.request_many([('X' * 128, 'OK'), ('INFO', 'INFO')]))
        assert len(commands) == previous_count
        started = time.monotonic()
        rejects(TimeoutError, lambda: eye.request_many([('INFO', 'INFO'), ('SILENT', 'OK')], timeout=.15))
        assert .15 <= time.monotonic() - started < .75, 'A batch shares one deadline'
        assert commands[previous_count:] == [b'INFO', b'SILENT'], 'Never replay a timed-out batch'
        assert eye.request('BOUNDARY', 'OK') == ['OK', 'x' * 124]
        rejects(OSError, lambda: eye.request('LONG', 'OK'))
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
print('PASS real pySerial over PTY: split ACK/POS across read timeouts, 128-byte boundary, overlong reply, no replay, handshake, bounds, errors, timeout, CLI')

with patch('pi.eye_control.socket.create_connection', side_effect=TimeoutError('timed out')) as connect:
    error = rejects(TimeoutError, lambda: TcpConnection('192.0.2.1'))
    assert 'TCP 建连超时' in str(error) and '尚未发送眼动指令' in str(error)
    connect.assert_called_once_with(('192.0.2.1', 4242), timeout=6)
fake_socket = Mock()
fake_socket.sendall.side_effect = TimeoutError('timed out')
with patch('pi.eye_control.socket.create_connection', return_value=fake_socket):
    with TcpConnection('192.0.2.1') as connection:
        error = rejects(TimeoutError, lambda: connection.authenticate('1' * 64))
        assert '认证请求发送超时' in str(error) and '1' * 64 not in str(error)
fake_socket.sendall.assert_called_once_with(('AUTH ' + '1' * 64 + '\n').encode())
fake_socket.recv.assert_not_called()
fake_socket.close.assert_called_once()
for failure in (TimeoutError('timed out'), serial.SerialTimeoutException('Write timeout')):
    connection = Mock()
    connection.write.side_effect = failure
    error = rejects(TimeoutError, lambda: EyeController(connection).request('EYE 2 Y 20', 'OK'))
    assert '指令发送超时' in str(error) and 'EYE 2 Y 20' in str(error) and '尚未确认' in str(error)
    connection.write.assert_called_once_with(b'EYE 2 Y 20\n')
    connection.read_until.assert_not_called()
print('PASS timeout stages: connect/auth/write identified, token hidden, no command replay')

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
listener.settimeout(0.1)
tcp_port = listener.getsockname()[1]
stop.clear()
token = '1' * 64  # Fake test credential, never used on a real device.
tcp_commands = []
pause_y_reply = False
delay_y_reply = False
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
                    if delay_y_reply:
                        connection.sendall(b'OK ')
                        time.sleep(2.2)
                        connection.sendall(f'{y}\n'.encode())
                        continue
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
            delay_y_reply = True
            previous_count = len(tcp_commands)
            started = time.monotonic()
            assert eye.set_y(20) == 20
            assert 2 < time.monotonic() - started < 5
            assert tcp_commands[previous_count:] == [b'Y 20'], 'Late reply must not replay the move'
            delay_y_reply = False
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
