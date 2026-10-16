#!/usr/bin/env python3
"""Local calibration UI; the ESP32 continues to render both physical TFTs."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
from pathlib import Path
import sys
import time

import serial

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pi.eye_control import EyeController, TcpConnection, parse_info2, read_token


def validate_backlight(data):
    if not isinstance(data, dict) or set(data) != {'screen', 'brightness'}:
        raise ValueError('背光参数不完整或包含未知字段')
    if type(data['screen']) is not int or data['screen'] not in (1, 2):
        raise ValueError('请选择屏幕 1 或 2')
    if type(data['brightness']) is not int or not 0 <= data['brightness'] <= 100:
        raise ValueError('背光亮度必须是 0 到 100 之间的整数')
    return data['screen'], data['brightness']


def validate(data):
    if not isinstance(data, dict) or set(data) - {'brightness'} != {'screen', 'center_x', 'center_y', 'angle', 'top'}:
        raise ValueError('校准参数不完整或包含未知字段')
    for key, low, high in (('screen', 1, 2), ('center_x', 0, 239), ('center_y', 0, 319)):
        if type(data[key]) is not int or not low <= data[key] <= high:
            raise ValueError(f'{key} 超出允许范围')
    if type(data['angle']) not in (int, float) or not -180 <= data['angle'] <= 180 or not math.isfinite(data['angle']):
        raise ValueError('旋转角度必须在 −180° 到 180° 之间')
    if data['top'] not in ('connector', 'opposite'):
        raise ValueError('无效的屏幕方向')
    if 'brightness' in data:
        validate_backlight({'screen': data['screen'], 'brightness': data['brightness']})
    return data['screen'], (data['center_x'], data['center_y'], round(data['angle'] * 10),
                            int(data['top'] == 'opposite'), 0)


def metadata(status, geometry):
    if len(status) != 6 or status[1] not in ('alia-eye-wifi-7', 'alia-eye-wifi-8', 'alia-eye-wifi-9', 'alia-eye-wifi-10'):
        raise RuntimeError('请先为 ESP32 烧录支持外部白底的 alia-eye-wifi-7/8/9/10 固件')
    width, height, *_ = parse_info2(geometry)
    if (width, height) != (240, 320):
        raise RuntimeError('当前校准界面需要 240×320 屏幕')
    return dict(firmware=status[1], mac=status[2], display=bool(int(status[3])), width=width, height=height)


def snapshot(info, replies):
    has_backlight = info['firmware'] == 'alia-eye-wifi-10'
    if len(replies) != (4 if has_backlight else 2):
        raise RuntimeError('需要两块屏幕的完整参数回复')
    eyes = []
    for screen, reply in enumerate(replies[:2], 1):
        if len(reply) != 8 or reply[:2] != ['CAL', str(screen)]:
            raise RuntimeError('无效的校准回复')
        cx, cy, angle, flipped, reserved, unsaved = map(int, reply[2:])
        item = dict(screen=screen, center_x=cx, center_y=cy, angle=angle / 10,
                    top='opposite' if flipped else 'connector')
        validate(item)
        if flipped not in (0, 1) or reserved != 0 or unsaved not in (0, 1):
            raise RuntimeError('无效的校准状态')
        eyes.append(dict(item, unsaved=bool(unsaved)))
    if has_backlight:
        for screen, reply in enumerate(replies[2:], 1):
            if len(reply) != 4 or reply[:2] != ['BL', str(screen)]:
                raise RuntimeError('无效的背光回复')
            try:
                percent, duty = map(int, reply[2:])
            except ValueError:
                raise RuntimeError('无效的背光数值') from None
            if not 0 <= percent <= 100 or duty != (1024 * percent + 50) // 100:
                raise RuntimeError('背光亮度与 PWM 读回不一致')
            eyes[screen - 1]['brightness'] = percent
    return dict(info, eyes=eyes)


identity_commands = [('STATUS', 'STATUS'), ('INFO2', 'INFO2')]
calibration_queries = [('EYE 1 CAL', 'CAL'), ('EYE 2 CAL', 'CAL')]
backlight_queries = [('EYE 1 BL', 'BL'), ('EYE 2 BL', 'BL')]


def state(eye):
    replies = eye.request_many(identity_commands + calibration_queries)
    info = metadata(*replies[:2])
    if info['firmware'] == 'alia-eye-wifi-10':
        replies += eye.request_many(backlight_queries)
    return snapshot(info, replies[2:])


class Device:
    def __init__(self, *, port=None, host=None, token_file=None):
        self.port, self.host = port, host
        self.token = read_token(token_file) if host else None
        self.serial = None
        self.tcp = self.info = None
        self.last_use = 0
        if port:
            link = serial.Serial(port=None, baudrate=115200, timeout=.1, write_timeout=1)
            link.dtr = link.rts = False
            link.port = port
            try:
                link.open()
                EyeController(link).connect()
            except Exception:
                link.close()
                raise
            self.serial = link

    def close(self):
        if self.serial:
            self.serial.close()
        self.close_network()

    def close_network(self):
        if self.tcp:
            self.tcp.close()
        self.tcp = self.info = None

    def expire_connection(self):
        if self.tcp and time.monotonic() - self.last_use >= .75:
            self.close_network()

    @contextmanager
    def controller(self):
        if self.serial:
            yield EyeController(self.serial)
        else:
            self.expire_connection()
            try:
                if self.tcp is None:
                    self.tcp = TcpConnection(self.host)
                    self.tcp.authenticate(self.token)
                yield EyeController(self.tcp)
            except Exception:
                self.close_network()  # An uncertain result ends this connection; never replay it.
                raise
            self.last_use = time.monotonic()

    def execute(self, route, data=None):
        if route == '/api/calibration':
            screen, values = validate(data)
        elif route == '/api/backlight':
            screen, brightness = validate_backlight(data)
        elif route == '/api/reset':
            if not isinstance(data, dict) or set(data) != {'screen'} or type(data['screen']) is not int or data['screen'] not in (1, 2):
                raise ValueError('请选择屏幕 1 或 2')
            screen = data['screen']
        elif route not in ('/api/state', '/api/save') or (route == '/api/save' and data != {}):
            raise ValueError('无效请求')
        with self.controller() as eye:
            if route == '/api/state':
                return state(eye)
            # A verified live TCP connection retains its device identity during one drag.
            info = self.info or metadata(*eye.request_many(identity_commands))
            if self.tcp:
                self.info = info
            has_backlight = info['firmware'] == 'alia-eye-wifi-10'
            if (route == '/api/backlight' or route == '/api/calibration' and 'brightness' in data) and not has_backlight:
                raise RuntimeError('背光调整需要 alia-eye-wifi-10 固件；当前设备尚未更新')
            queries = calibration_queries + (backlight_queries if has_backlight else [])
            if route == '/api/backlight':
                replies = eye.request_many([(f'EYE {screen} BL {brightness}', 'OKBL')] + queries)
                if replies[0] != ['OKBL', str(screen), str(brightness)]:
                    raise RuntimeError('设备未确认背光亮度')
                result = snapshot(info, replies[1:])
                if result['eyes'][screen - 1]['brightness'] != brightness:
                    raise RuntimeError('背光读回与请求不一致')
                return result
            if route in ('/api/calibration', '/api/reset'):
                command = 'CAL ' + ' '.join(map(str, values)) if route == '/api/calibration' else 'RESETCAL'
                requests = [(f'EYE {screen} XY 0 0', 'OKXY'), (f'EYE {screen} {command}', 'OKCAL')]
                expected = [['OKXY', str(screen), '0', '0'], ['OKCAL', str(screen)]]
                if route == '/api/calibration' and 'brightness' in data:
                    requests.append((f'EYE {screen} BL {data["brightness"]}', 'OKBL'))
                    expected.append(['OKBL', str(screen), str(data['brightness'])])
                requests += queries
                sync = info['firmware'] in ('alia-eye-wifi-9', 'alia-eye-wifi-10')
                if sync:
                    requests.append(('SYNC', 'OKSYNC'))
                replies = eye.request_many(requests)
                if replies[:len(expected)] != expected:
                    raise RuntimeError('设备未确认校准参数')
                if sync and replies[-1] != ['OKSYNC']:
                    raise RuntimeError('设备未确认本轮画面传输完成')
                result = snapshot(info, replies[len(expected):len(expected) + len(queries)])
                if route == '/api/calibration' and 'brightness' in data and result['eyes'][screen - 1]['brightness'] != data['brightness']:
                    raise RuntimeError('背光读回与请求不一致')
                return result
            replies = eye.request_many([('SAVECAL', 'OKSAVE')] + queries)
            if replies[0] != ['OKSAVE']:
                raise RuntimeError('设备未确认保存')
            return snapshot(info, replies[1:])


def make_handler(device):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, code, body, kind='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def local_request(self):
            port = self.server.server_address[1]
            host = self.headers.get('Host', '')
            return host in (f'127.0.0.1:{port}', f'localhost:{port}') and self.headers.get('Origin', f'http://{host}') == f'http://{host}'

        def do_GET(self):
            if not self.local_request():
                self.reply(403, {'error': '只允许本机同源访问'})
            elif self.path == '/':
                self.reply(200, (ROOT / 'pi/calibration.html').read_bytes(), 'text/html; charset=utf-8')
            elif self.path == '/eye.png':
                self.reply(200, (ROOT / 'assets/eye-texture.png').read_bytes(), 'image/png')
            elif self.path == '/api/state':
                self.device_request('/api/state')
            else:
                self.reply(404, {'error': '未找到'})

        def device_request(self, route, data=None):
            try:
                self.reply(200, device.execute(route, data))
            except ValueError as error:
                self.reply(400, {'error': str(error)})
            except (RuntimeError, OSError) as error:
                self.reply(502, {'error': f'{error}；应用结果可能尚未确认，请重新读取设备状态'})

        def do_POST(self):
            if not self.local_request() or self.headers.get('X-Eye-Control') != '1':
                self.reply(403, {'error': '只允许本机同源控制'})
                return
            if self.path not in ('/api/calibration', '/api/backlight', '/api/reset', '/api/save'):
                self.reply(404, {'error': '未找到'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.headers.get_content_type() != 'application/json' or not 0 < length <= 2048:
                    raise ValueError('请求必须是小于 2048 字节的 JSON')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('请求不完整')
                def invalid_constant(_):
                    raise ValueError('JSON 不能包含 NaN 或 Infinity')
                data = json.loads(raw, parse_constant=invalid_constant)
            except (ValueError, UnicodeDecodeError, TimeoutError) as error:
                self.reply(400, {'error': str(error)})
                return
            self.device_request(self.path, data)

        def log_message(self, format, *args):
            pass  # Request bodies and credentials never enter access logs.
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument('--port', help='ESP32 USB serial port')
    transport.add_argument('--host', help='ESP32 Wi-Fi address')
    parser.add_argument('--token-file', help='Existing private TCP control token')
    parser.add_argument('--web-port', type=int, default=8080)
    args = parser.parse_args()
    if not 1 <= args.web_port <= 65535 or bool(args.host) != bool(args.token_file):
        parser.error('Web 端口必须为 1..65535；Wi-Fi 模式需要 --token-file，USB 模式不使用令牌')
    device = None
    try:
        # Bind first: a second launcher must not reset USB before reporting a busy port.
        with HTTPServer(('127.0.0.1', args.web_port), BaseHTTPRequestHandler) as server:
            device = Device(port=args.port, host=args.host, token_file=args.token_file)
            # Single-threaded HTTP serializes device commands from all browser requests.
            server.RequestHandlerClass = make_handler(device)
            server.service_actions = device.expire_connection
            print(f'校准界面：http://127.0.0.1:{args.web_port}/  （Ctrl+C 关闭）', flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f'启动失败：{error}\n')
    finally:
        if device:
            device.close()


if __name__ == '__main__':
    main()
