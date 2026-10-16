#!/usr/bin/env python3
"""Provision ESP32 Wi-Fi over USB without putting credentials in command arguments."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
import warnings

import serial


def private_text(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'r', encoding='utf-8') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError('凭据文件必须是当前用户拥有的普通 0600 文件')
        text = source.read(4097)
    if len(text) > 4096:
        raise ValueError('凭据文件过大')
    return text


def load_credentials(path):
    try:
        values = json.loads(private_text(path))
    except json.JSONDecodeError:
        raise ValueError('Wi-Fi 文件不是有效 JSON') from None
    if not isinstance(values, dict) or set(values) != {'ssid', 'password'}:
        raise ValueError('Wi-Fi 文件必须包含 ssid 和 password 两个字段')
    validate_wifi(values['ssid'], values['password'])
    return values['ssid'], values['password']


def validate_wifi(ssid, password):
    if not isinstance(ssid, str) or not isinstance(password, str):
        raise ValueError('SSID 和密码必须是文本')
    if '\0' in ssid or not 1 <= len(ssid.encode('utf-8')) <= 32:
        raise ValueError('SSID 必须为 1 至 32 个 UTF-8 字节且不能含 NUL')
    if '\0' in password or not (8 <= len(password.encode('utf-8')) <= 63 or re.fullmatch('[0-9a-fA-F]{64}', password)):
        raise ValueError('Wi-Fi 密码必须为 8 至 63 个 UTF-8 字节或 64 位十六进制 PSK')


def load_token(path):
    token = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = private_text(path).strip()
    else:
        with os.fdopen(fd, 'w', encoding='ascii') as destination:
            destination.write(token + '\n')
    if not re.fullmatch('[0-9a-f]{64}', token):
        raise ValueError('控制 token 文件必须包含 64 位小写十六进制 token')
    return token


def config_packet(ssid, password, token):
    validate_wifi(ssid, password)
    if not re.fullmatch('[0-9a-f]{64}', token):
        raise ValueError('控制 token 格式错误')
    return f'CONFIG {ssid.encode("utf-8").hex()} {password.encode("utf-8").hex()} {token}\n'.encode('ascii')


def wait_reply(link, prefix, pending, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending.extend(link.read_until(b'\n', 128))
        if len(pending) > 256:
            raise RuntimeError('ESP32 响应超长')
        if not pending.endswith(b'\n'):
            continue
        try:
            fields = pending.decode('ascii').strip().split()
        except UnicodeDecodeError:
            fields = []
        pending.clear()
        if fields and fields[0] == 'ERR':
            # Device error text may echo secrets; only expose the error category.
            raise RuntimeError('ESP32 拒绝请求（ERR）')
        if fields and fields[0] == prefix:
            return fields
    raise TimeoutError(f'ESP32 未回应 {prefix}；配置结果可能未知，请读取状态')


def send(link, packet):
    if link.write(packet) != len(packet):
        raise OSError('串口写入不完整')


def provision(link, ssid, password, token, expect_mac=None, timeout=30):
    packet = config_packet(ssid, password, token)
    if expect_mac is not None and not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', expect_mac):
        raise ValueError('预期 MAC 格式错误')
    pending = bytearray()
    deadline = time.monotonic() + 5
    while True:
        send(link, b'STATUS\n')
        try:
            fields = wait_reply(link, 'STATUS', pending, 0.5)
            break
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise
    if len(fields) != 6 or fields[1] not in tuple(f'alia-eye-wifi-{version}' for version in range(2, 11)) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', fields[2]):
        raise RuntimeError('无效 STATUS 响应或固件版本不符')
    try:
        display, target, drawn = map(int, fields[3:])
    except ValueError:
        raise RuntimeError('无效 STATUS 数值') from None
    if display not in (0, 1) or not all(-(2**31) <= v < 2**31 for v in (target, drawn)):
        raise RuntimeError('无效 STATUS 数值')
    if expect_mac is not None and fields[2].lower() != expect_mac.lower():
        raise RuntimeError('设备 MAC 不匹配，未发送 Wi-Fi 配置')
    mac = fields[2]
    send(link, packet)
    if wait_reply(link, 'OK', pending, 2) != ['OK', 'CONFIG']:
        raise RuntimeError('无效 CONFIG 确认')
    fields = wait_reply(link, 'NET', pending, timeout)
    if len(fields) != 3 or fields[2] != '4242':
        raise RuntimeError('无效 NET 响应')
    try:
        address = ipaddress.IPv4Address(fields[1])
    except ipaddress.AddressValueError:
        raise RuntimeError('无效 NET IPv4 地址') from None
    if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address('255.255.255.255'):
        raise RuntimeError('NET 返回了不可用地址')
    return mac, display, str(address), 4242


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='明确指定 ESP32 串口')
    wifi = parser.add_mutually_exclusive_group(required=True)
    wifi.add_argument('--ssid', help='密码通过交互提示输入，不接受密码命令行参数')
    wifi.add_argument('--wifi-file', type=Path, help='当前用户拥有的 0600 JSON 文件')
    parser.add_argument('--token-file', required=True, type=Path, help='新建 0600 token 文件；已有有效 token 保持不变')
    parser.add_argument('--expect-mac', help='配置前核对 ESP32 MAC')
    args = parser.parse_args()
    try:
        if args.wifi_file is not None:
            ssid, password = load_credentials(args.wifi_file)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                try:
                    password = getpass.getpass('Wi-Fi 密码：')
                except getpass.GetPassWarning:
                    raise ValueError('没有可隐藏密码的交互终端；请使用私有 --wifi-file') from None
            ssid = args.ssid
            validate_wifi(ssid, password)
        token = load_token(args.token_file)
        with serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=1) as link:
            link.dtr = link.rts = False
            link.port = args.port
            link.open()
            mac, display, address, port = provision(link, ssid, password, token, args.expect_mac)
        print(f'ESP32 {mac}: Wi-Fi 已连接，NET {address} {port}，display={display}')
        print(f'控制 token 已保存于 {args.token_file}；OK CONFIG 表示配置已保存，NET 表示已获 IP')
    except (serial.SerialException, OSError, TimeoutError, RuntimeError, ValueError) as error:
        parser.exit(1, f'错误：{error}\n')


if __name__ == '__main__':
    main()
