#!/usr/bin/env python3
"""Raspberry Pi -> ESP32 over Wi-Fi TCP or USB serial; rendering stays on ESP32."""
import argparse
import curses
import math
import os
import signal
import socket
import stat
import sys
import time

import serial


def read_token(path):
    with open(path, 'r', encoding='ascii') as secret:
        metadata = os.fstat(secret.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
            raise ValueError('认证文件必须是权限 0600 的普通文件')
        if metadata.st_uid != os.getuid():
            raise ValueError('认证文件必须由当前用户拥有')
        token = secret.read(66).removesuffix('\n')
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('认证文件必须包含 64 位小写十六进制 token')
    return token


def parse_info2(reply):
    if len(reply) != 7 or reply[0] != 'INFO2':
        raise RuntimeError('无效 INFO2 响应')
    width, height, min_x, max_x, min_y, max_y = map(int, reply[1:])
    if not (1 <= width < height <= 4096 and
            -4096 <= min_x <= 0 <= max_x <= 4096 and
            -4096 <= min_y <= 0 <= max_y <= 4096):
        raise RuntimeError('无效屏幕尺寸或二维眼动范围')
    return width, height, min_x, max_x, min_y, max_y


class TcpConnection:
    def __init__(self, host, port=4242):
        try:
            self.socket = socket.create_connection((host, port), timeout=6)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except TimeoutError as error:
            raise TimeoutError(f'ESP32 TCP 建连超时（{host}:{port}，6 秒）；本次尚未发送眼动指令') from error
        self.pending = bytearray()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.socket.close()

    def write(self, packet):
        self.socket.settimeout(1)
        self.socket.sendall(packet)
        return len(packet)

    def read_until(self, expected=b'\n', size=128):
        deadline = time.monotonic() + 0.1
        while True:
            end = self.pending.find(expected)
            if end >= 0:
                end += len(expected)
                if end > size:
                    raise OSError('TCP 响应行过长')
                line = bytes(self.pending[:end])
                del self.pending[:end]
                return line
            if len(self.pending) >= size:
                raise OSError('TCP 响应行过长')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b''
            self.socket.settimeout(remaining)
            try:
                packet = self.socket.recv(size)
            except socket.timeout:
                return b''
            if not packet:
                raise OSError('ESP32 TCP 连接已断开')
            self.pending.extend(packet)

    def authenticate(self, token):
        try:
            self.write(('AUTH ' + token + '\n').encode('ascii'))
        except TimeoutError as error:
            raise TimeoutError('ESP32 认证请求发送超时；本次尚未发送眼动指令') from error
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            reply = self.read_until()
            if reply:
                if reply.strip() != b'OK AUTH':
                    raise RuntimeError('ESP32 网络认证失败')
                return
        raise TimeoutError('ESP32 网络认证回复超时；本次尚未发送眼动指令')


class EyeController:
    def __init__(self, connection):
        self.connection = connection
        self.min_x = self.max_x = None
        self.min_y = self.max_y = None

    # Allow a lost TCP reply to be retransmitted before giving up; never replay a move.
    def request(self, command, prefix, timeout=5.0):
        return self.request_many([(command, prefix)], timeout)[0]

    def request_many(self, requests, timeout=5.0):
        if not requests:
            raise ValueError('批量请求不能为空')
        packet = ''.join(command + '\n' for command, _ in requests).encode('ascii')
        # Keep a batch within the ESP32's network processing budget before it draws.
        if len(requests) > 1 and len(packet) > 128:
            raise ValueError('批量请求不能超过 128 字节')
        label = requests[0][0] if len(requests) == 1 else '批量指令'
        try:
            written = self.connection.write(packet)
        except (TimeoutError, serial.SerialTimeoutException) as error:
            raise TimeoutError(f'ESP32 指令发送超时：{label!r}；是否执行尚未确认') from error
        if written != len(packet):
            raise OSError('指令写入不完整')
        deadline = time.monotonic() + timeout
        replies = []
        for command, prefix in requests:
            pending = bytearray()
            while time.monotonic() < deadline:
                # pySerial returns consumed partial lines when an individual read times out.
                pending.extend(self.connection.read_until(b'\n', 128 - len(pending)))
                complete = pending.endswith(b'\n')
                if len(pending) > 128 or (len(pending) == 128 and not complete):
                    raise OSError('ESP32 响应行过长')
                if not complete:
                    continue
                fields = pending.decode('ascii', errors='replace').strip().split()
                pending.clear()
                if fields and fields[0] == 'ERR':
                    raise RuntimeError('ESP32: ' + ' '.join(fields[1:]))
                if fields and fields[0] == prefix:
                    replies.append(fields)
                    break
            else:
                raise TimeoutError(f'ESP32 未回应 {command!r}；是否执行尚未确认，请检查连接后读取位置')
        return replies

    def connect(self):
        deadline = time.monotonic() + 5
        while True:
            try:
                reply = self.request('INFO', 'INFO', timeout=0.5)
                break
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
        if len(reply) != 5:
            raise RuntimeError('无效 INFO 响应')
        width, height, low, high = map(int, reply[1:])
        if not (1 <= width < height <= 4096 and -4096 <= low <= 0 <= high <= 4096):
            raise RuntimeError('无效屏幕尺寸或眼动范围')
        self.min_y, self.max_y = low, high
        return width, height, low, high

    def request_eye(self, command, prefix, screen=None):
        if screen is not None and (type(screen) is not int or screen not in (1, 2)):
            raise ValueError('屏幕编号必须为 1 或 2；None 表示双眼')
        reply = self.request(command if screen is None else f'EYE {screen} {command}', prefix)
        if screen is not None:
            if len(reply) < 2 or reply[1] != str(screen):
                raise RuntimeError('ESP32 回复的屏幕编号不匹配')
            reply = [reply[0], *reply[2:]]
        return reply

    def connect_eyes(self):
        if self.request('EYES', 'EYES') != ['EYES', '2']:
            raise RuntimeError('此功能需要双屏固件 alia-eye-wifi-4')

    def set_y(self, pixels, screen=None):
        if type(pixels) is not int or not -(2**31) <= pixels < 2**31:
            raise ValueError('位移必须是 32 位有符号整数')
        if self.min_y is None:
            raise RuntimeError('先调用 connect()')
        expected = min(self.max_y, max(self.min_y, pixels))
        reply = self.request_eye(f'Y {pixels}', 'OK', screen)
        if len(reply) != 2 or int(reply[1]) != expected:
            raise RuntimeError('ESP32 位移确认值不匹配')
        return expected

    def connect_2d(self):
        width, height, min_x, max_x, min_y, max_y = parse_info2(self.request('INFO2', 'INFO2'))
        self.min_x, self.max_x = min_x, max_x
        self.min_y, self.max_y = min_y, max_y
        return width, height, min_x, max_x, min_y, max_y

    def set_xy(self, x, y, screen=None):
        if any(type(value) is not int or not -(2**31) <= value < 2**31 for value in (x, y)):
            raise ValueError('位移必须是 32 位有符号整数')
        if self.min_x is None:
            raise RuntimeError('先调用 connect_2d()')
        expected = (min(self.max_x, max(self.min_x, x)), min(self.max_y, max(self.min_y, y)))
        reply = self.request_eye(f'XY {x} {y}', 'OKXY', screen)
        if len(reply) != 3 or tuple(map(int, reply[1:])) != expected:
            raise RuntimeError('ESP32 二维位移确认值不匹配')
        return expected


def manual_control(eye, selection='both'):
    stopped = False

    def stop_manual(_signal, _frame):
        nonlocal stopped
        stopped = True

    def screen_loop(screen):
        selected = selection
        screen.timeout(100)
        positions = [eye.set_xy(0, 0)] * 2
        directions = [1, 1]
        moves = {curses.KEY_LEFT: (-10, 0), 'a': (-10, 0),
                 curses.KEY_RIGHT: (10, 0), 'd': (10, 0),
                 curses.KEY_UP: (0, -10), 'w': (0, -10),
                 curses.KEY_DOWN: (0, 10), 's': (0, 10)}
        last_request = time.monotonic()
        while not stopped:
            indices = (0, 1) if selected == 'both' else (int(selected) - 1,)
            lines = [f'1: eye 1 | 2: eye 2 | 0: both | Selected: {selected}',
                     'Arrow keys / WASD: move 10 px; Space: center selection',
                     'R: reverse selected axes; Q / Ctrl+C: center BOTH and quit',
                     *[f'Eye {i + 1}: Position x={p[0]:+d} y={p[1]:+d} px | Direction: {"default" if directions[i] == 1 else "reversed"}'
                       for i, p in enumerate(positions)],
                     f'Bounds x=[{eye.min_x}, {eye.max_x}] y=[{eye.min_y}, {eye.max_y}]']
            screen.erase()
            rows, columns = screen.getmaxyx()
            for row, line in enumerate(lines[:rows]):
                screen.addnstr(row, 0, line, max(0, columns - 1))
            screen.refresh()
            key = screen.getch()
            if stopped:
                break
            if 0 <= key < 256:
                key = chr(key).lower()
            if key == 'q':
                break
            if key in ('0', '1', '2'):
                selected = 'both' if key == '0' else key
            elif key == 'r':
                for i in indices:
                    directions[i] = -directions[i]
            elif key == ' ' or key in moves:
                dx, dy = moves.get(key, (0, 0))
                targets = [(0, 0) if key == ' ' else
                           (positions[i][0] + directions[i] * dx, positions[i][1] + directions[i] * dy)
                           for i in indices]
                if len(targets) == 2 and targets[0] == targets[1]:
                    positions[:] = [eye.set_xy(*targets[0])] * 2
                else:
                    for i, target in zip(indices, targets):
                        positions[i] = eye.set_xy(*target, screen=i + 1)
                last_request = time.monotonic()
            if time.monotonic() - last_request >= 5:
                eye.connect_2d()
                last_request = time.monotonic()
        eye.set_xy(0, 0)

    # A stop signal must not interrupt the outstanding ACK before the center command.
    previous_handler = signal.signal(signal.SIGINT, stop_manual)
    try:
        curses.wrapper(screen_loop)
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument('--port', help='/dev/ttyUSB0 or /dev/serial/by-id/...')
    transport.add_argument('--host', help='ESP32 Wi-Fi IP address or hostname')
    parser.add_argument('--tcp-port', type=int, default=4242)
    parser.add_argument('--token-file', help='Local authentication file with permissions 0600')
    parser.add_argument('--screen', choices=('1', '2', 'both'), default='both',
                        help='Target eye for commands/demo, or initial manual selection (default: both)')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--y', type=int, help='Signed displacement along the configured eye-motion axis; zero = center')
    mode.add_argument('--demo', action='store_true', help='Smooth vertical oscillation, then return to center')
    mode.add_argument('--xy', type=int, nargs=2, metavar=('X', 'Y'), help='Set the two-dimensional eye position')
    mode.add_argument('--manual', action='store_true', help='Arrow keys/WASD control; Q or Ctrl+C centers and exits')
    parser.add_argument('--amplitude', type=int, default=60)
    parser.add_argument('--period', type=float, default=4.0)
    parser.add_argument('--seconds', type=float, default=10.0)
    args = parser.parse_args()
    if not (args.manual or args.xy is not None) and (
            args.amplitude < 0 or any(not math.isfinite(v) or v <= 0 for v in (args.period, args.seconds))):
        parser.error('振幅必须非负；周期和时长必须为有限正数')
    if not 1 <= args.tcp_port <= 65535:
        parser.error('TCP 端口必须在 1 到 65535 之间')
    if args.host and not args.token_file:
        parser.error('--host 需要 --token-file')
    if args.port and args.token_file:
        parser.error('--token-file 仅适用于 --host')
    if args.manual and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('--manual 需要交互终端；SSH 请使用 -tt')
    try:
        token = read_token(args.token_file) if args.host else None
        with (TcpConnection(args.host, args.tcp_port) if args.host else
              serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=1)) as link:
            if args.host:
                link.authenticate(token)
            else:
                # Open with DTR/RTS inactive to avoid deliberately resetting an attached ESP32.
                link.dtr = link.rts = False
                link.port = args.port
                link.open()
            eye = EyeController(link)
            selected = None if args.screen == 'both' else int(args.screen)
            if args.manual or selected is not None:
                eye.connect_eyes()
            if args.manual or args.xy is not None:
                width, height, min_x, max_x, min_y, max_y = eye.connect_2d()
                print(f'ESP32: {width}x{height}, x=[{min_x}, {max_x}], y=[{min_y}, {max_y}] px')
                if args.xy is not None:
                    x, y = eye.set_xy(*args.xy, screen=selected)
                    print(f'OK x={x} y={y} px')
                else:
                    manual_control(eye, args.screen)
                    print('手动控制结束，已发送居中指令')
                return
            width, height, low, high = eye.connect()
            print(f'ESP32: {width}x{height}, y=[{low}, {high}] px')
            if args.y is not None:
                print(f'OK y={eye.set_y(args.y, screen=selected)} px')
                return
            if args.amplitude > min(-low, high):
                parser.error(f'此固件的往返振幅上限为 {min(-low, high)} px')
            start = time.monotonic()
            stopped = False

            def stop_demo(_signal, _frame):
                nonlocal stopped
                stopped = True

            # Finish the pending reply before centering, so its ACK cannot be mistaken for zero.
            previous_handler = signal.signal(signal.SIGINT, stop_demo)
            try:
                while not stopped and time.monotonic() - start < args.seconds:
                    phase = (time.monotonic() - start) * 2 * math.pi / args.period
                    eye.set_y(round(args.amplitude * math.sin(phase)), screen=selected)
                    time.sleep(1 / 30)
                eye.set_y(0, screen=selected)
            finally:
                signal.signal(signal.SIGINT, previous_handler)
            print('演示结束，已发送居中指令')
    except (serial.SerialException, OSError, TimeoutError, RuntimeError, ValueError, curses.error) as error:
        parser.exit(1, f'错误：{error}\n')


if __name__ == '__main__':
    main()
