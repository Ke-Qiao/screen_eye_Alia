#!/usr/bin/env python3
"""Manual controls against simulated TCP firmware and a real controlling terminal."""
import fcntl
import os
from pathlib import Path
import pty
import queue
import select
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pi.eye_control import EyeController, TcpConnection

TOKEN = '4' * 64  # Synthetic credential for the loopback simulator only.
INFO = b'INFO2 240 320 -213 213 -197 198\n'


class Device:
    def __init__(self, initial=(0, 0), delay_ack=False, reject_xy=False):
        self.initial, self.delay_ack, self.reject_xy = initial, delay_ack, reject_xy
        self.commands = queue.Queue()
        self.release = threading.Event()
        self.stop = threading.Event()
        self.error = None
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                positions = [self.initial, self.initial]
                with conn, conn.makefile('rb') as stream:
                    for packet in stream:
                        command = packet.rstrip(b'\n')
                        self.commands.put(command)
                        selected = None
                        if command.startswith(b'EYE '):
                            _, number, command = command.split(b' ', 2)
                            selected = int(number) - 1
                            assert selected in (0, 1)
                        tag = '' if selected is None else f'{selected + 1} '
                        indices = (0, 1) if selected is None else (selected,)
                        if command == ('AUTH ' + TOKEN).encode():
                            reply = b'OK AUTH\n'
                        elif command == b'EYES':
                            reply = b'EYES 2\n'
                        elif command == b'INFO':
                            reply = b'INFO 240 320 -197 198\n'
                        elif command == b'INFO2':
                            reply = INFO
                        elif command == b'POS':
                            x, y = positions[indices[0]]
                            reply = f'POS {tag}{x} {y} {x} {y}\n'.encode()
                        elif command.startswith(b'XY '):
                            _, a, b = command.split()
                            x, y = min(213, max(-213, int(a))), min(198, max(-197, int(b)))
                            if self.delay_ack and (x, y) != (0, 0):
                                self.delay_ack = False
                                assert self.release.wait(3), 'Test did not release delayed ACK'
                            for i in indices:
                                positions[i] = (x, y)
                            reply = b'ERR test\n' if self.reject_xy and (x, y) != (0, 0) else f'OKXY {tag}{x} {y}\n'.encode()
                        elif command.startswith(b'Y '):
                            y = min(198, max(-197, int(command[2:])))
                            for i in indices:
                                positions[i] = (positions[i][0], y)
                            reply = f'OK {tag}{y}\n'.encode()
                        else:
                            reply = b'ERR command\n'
                        conn.sendall(reply)
        except BaseException as error:
            self.error = error

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, kind, *_):
        self.stop.set()
        self.release.set()
        self.thread.join(2)
        self.listener.close()
        assert not self.thread.is_alive(), 'Simulator connection was not closed'
        if kind is None and self.error:
            raise self.error


class Terminal:
    def __init__(self, device, token_file, rows=24, columns=100):
        self.device = device
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        self.output = bytearray()
        done_read, self.done_write = os.pipe()

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.process = subprocess.Popen(
            ['/bin/sh', '-c',
             'trap "" INT; "$@"; code=$?; printf "\\nEYE_TEST_EXIT=%s\\n" "$code"; '
             f'read -r test_done <&{done_read}; exit "$code"', 'manual-test',
             sys.executable, '-u', str(ROOT / 'pi/eye_control.py'), '--host', '127.0.0.1',
             '--tcp-port', str(device.port), '--token-file', str(token_file), '--manual', '--amplitude', '999999'],
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            env=dict(os.environ, TERM='xterm-256color'), preexec_fn=controlling_terminal,
            pass_fds=(done_read,))
        os.close(done_read)

    def drain(self):
        while select.select([self.master], [], [], 0)[0]:
            packet = os.read(self.master, 4096)
            if not packet:
                break
            self.output.extend(packet)

    def expect(self, command, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            try:
                actual = self.device.commands.get(timeout=.05)
                assert actual == command, (actual, command, bytes(self.output))
                return
            except queue.Empty:
                assert self.process.poll() is None, bytes(self.output)
        raise AssertionError(('Missing command', command, bytes(self.output)))

    def key(self, key, command):
        os.write(self.master, key)
        self.expect(command)

    def finish(self, code=0):
        # Keep the session leader alive: macOS revokes the PTY when it exits.
        deadline = time.monotonic() + 3
        marker = f'EYE_TEST_EXIT={code}'.encode()
        while marker not in self.output and time.monotonic() < deadline:
            self.drain()
            time.sleep(.02)
        assert marker in self.output, bytes(self.output)
        restored = termios.tcgetattr(self.slave)
        # PENDIN is macOS's transient input-retype flag, not a terminal mode.
        restored[3] &= ~getattr(termios, 'PENDIN', 0)
        assert restored == self.original, ('Terminal settings not restored', restored, self.original)
        os.write(self.done_write, b'\n')
        assert self.process.wait(timeout=3) == code, bytes(self.output)
        self.drain()

    def __enter__(self):
        self.expect(('AUTH ' + TOKEN).encode())
        self.expect(b'EYES')
        self.expect(b'INFO2')
        self.expect(b'XY 0 0')
        return self

    def __exit__(self, *_):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        os.close(self.done_write)
        os.close(self.master)
        os.close(self.slave)


def rejects(error, action):
    try:
        action()
    except error:
        return
    raise AssertionError(f'Expected {error.__name__}')


for invalid in ((True, 0), (1.5, 0), (0, 2**31), (-(2**31)-1, 0)):
    rejects(ValueError, lambda: EyeController(None).set_xy(*invalid))
rejects(RuntimeError, lambda: EyeController(None).set_xy(0, 0))


class Reply:
    def __init__(self, response):
        self.response = response

    def write(self, packet):
        return len(packet)

    def read_until(self, *_):
        return self.response


for invalid_info in (b'INFO2 240 320 -213 213 1 198\n', b'INFO2 320 240 -213 213 -197 198\n',
                     b'INFO2 240 320 -213 213\n'):
    rejects(RuntimeError, lambda: EyeController(Reply(invalid_info)).connect_2d())
eye = EyeController(Reply(INFO))
eye.connect_2d()
eye.connection = Reply(b'OKXY 10 0\n')
rejects(RuntimeError, lambda: eye.set_xy(0, 0))
for invalid in (0, 3, True, '1', 1.0):
    rejects(ValueError, lambda: eye.set_xy(0, 0, screen=invalid))
eye.connection = Reply(b'OKXY 2 0 0\n')
rejects(RuntimeError, lambda: eye.set_xy(0, 0, screen=1))
eye.connection = Reply(b'EYES 1\n')
rejects(RuntimeError, eye.connect_eyes)

with tempfile.TemporaryDirectory(prefix='alia-manual-') as directory:
    token_file = Path(directory) / 'token'
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    with Device() as device:
        with TcpConnection('127.0.0.1', device.port) as link:
            link.authenticate(TOKEN)
            eye = EyeController(link)
            assert eye.connect_2d() == (240, 320, -213, 213, -197, 198)
            assert eye.set_xy(-1000, 1000) == (-213, 198)
            assert eye.set_xy(1000, -1000) == (213, -197)
            assert eye.set_xy(0, 0) == (0, 0)
        result = subprocess.run(
            [sys.executable, str(ROOT / 'pi/eye_control.py'), '--host', '127.0.0.1',
             '--tcp-port', str(device.port), '--token-file', str(token_file), '--xy', '1000', '-1000',
             '--amplitude', '-1', '--period', 'nan'],
            capture_output=True, text=True, timeout=4)
        assert result.returncode == 0 and 'OK x=213 y=-197 px' in result.stdout, result
    print('PASS 2D client and --xy: bounds, clamping, input validation')

    with Device() as device:
        with TcpConnection('127.0.0.1', device.port) as link:
            link.authenticate(TOKEN)
            eye = EyeController(link)
            eye.connect_eyes()
            eye.connect_2d()
            assert eye.set_xy(30, -40, screen=1) == (30, -40)
            assert eye.set_xy(-1000, 1000, screen=2) == (-213, 198)
            assert eye.set_y(20, screen=2) == 20
            assert eye.request_eye('POS', 'POS', 1) == ['POS', '30', '-40', '30', '-40']
            assert eye.request_eye('POS', 'POS', 2) == ['POS', '-213', '20', '-213', '20']
        for mode in (['--xy', '20', '-10'], ['--y', '10'],
                     ['--demo', '--seconds', '.1', '--period', '1']):
            result = subprocess.run(
                [sys.executable, str(ROOT / 'pi/eye_control.py'), '--host', '127.0.0.1',
                 '--tcp-port', str(device.port), '--token-file', str(token_file), '--screen', '2', *mode],
                capture_output=True, text=True, timeout=4)
            assert result.returncode == 0, result
        commands = list(device.commands.queue)
        assert b'EYE 2 XY 20 -10' in commands and b'EYE 2 Y 10' in commands and b'EYE 2 Y 0' in commands
        assert not any(c.startswith((b'XY ', b'Y ')) for c in commands)
    print('PASS independent eyes: screen IDs, no cross-eye changes, per-eye XY/Y/demo CLI')

    with Device() as device, Terminal(device, token_file) as terminal:
        terminal.key(b'1d', b'EYE 1 XY 10 0')
        terminal.key(b'2w', b'EYE 2 XY 0 -10')
        terminal.key(b'0s', b'EYE 1 XY 10 10')
        terminal.expect(b'EYE 2 XY 0 0')
        terminal.key(b'2rd', b'EYE 2 XY -10 0')
        terminal.key(b'1d', b'EYE 1 XY 20 10')
        terminal.key(b' ', b'EYE 1 XY 0 0')
        terminal.key(b'q', b'XY 0 0')
        terminal.finish()
    print('PASS real PTY: eye selection, independent offsets/reversal, both-eye relative motion, selected center')

    with Device(initial=(200, 190)) as device, Terminal(device, token_file) as terminal:
        for key, command in [(b'\x1bOC', b'XY 10 0'), (b'\x1bOB', b'XY 10 10'),
                             (b'\x1bOD', b'XY 0 10'), (b'\x1bOA', b'XY 0 0'),
                             (b'w', b'XY 0 -10'), (b'a', b'XY -10 -10'),
                             (b's', b'XY -10 0'), (b'd', b'XY 0 0'),
                             (b'rd', b'XY -10 0'), (b'w', b'XY -10 10'), (b' ', b'XY 0 0')]:
            terminal.key(key, command)
        for index in range(1, 23):
            terminal.key(b'd', f'XY {-10 * index} 0'.encode())
        terminal.key(b'd', b'XY -223 0')
        terminal.key(b' ', b'XY 0 0')
        start = time.monotonic()
        terminal.expect(b'INFO2', timeout=7)
        assert time.monotonic() - start >= 4.5, 'Idle heartbeat was not spaced at five seconds'
        terminal.key(b'q', b'XY 0 0')
        terminal.finish()
        assert b'Position x=' in terminal.output and b'reversed' in terminal.output
    print('PASS real PTY: arrows/WASD, bounds, reversal, space, idle heartbeat, Q centering, terminal restore')

    with Device(delay_ack=True) as device, Terminal(device, token_file) as terminal:
        terminal.key(b'd', b'XY 10 0')
        os.write(terminal.master, b'\x03')  # The controlling PTY generates the actual SIGINT.
        time.sleep(.1)
        assert terminal.process.poll() is None and device.commands.empty(), 'SIGINT interrupted pending ACK'
        device.release.set()
        terminal.expect(b'XY 0 0')
        terminal.finish()
    print('PASS real Ctrl+C during pending ACK: finish response, center once, clean exit, terminal restore')

    with Device(reject_xy=True) as device, Terminal(device, token_file, rows=2, columns=18) as terminal:
        terminal.key(b'd', b'XY 10 0')
        terminal.finish(code=1)
        assert device.commands.empty(), 'Failed action was retried'
    print('PASS protocol error: no retry and terminal restored')
