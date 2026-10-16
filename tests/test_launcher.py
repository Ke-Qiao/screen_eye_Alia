#!/usr/bin/env python3
"""Check the Finder launcher with a fake SSH executable; no devices or network."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
expected_args = [
    '-tt', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
    'ke-qiao@192.168.10.221',
    'python3 -u /home/ke-qiao/Alia-eye/pi/eye_control.py --host 192.168.10.133 '
    '--token-file /home/ke-qiao/Alia-eye/eye-control.token ',
]
with tempfile.TemporaryDirectory(prefix='alia-launcher-') as directory:
    temp = Path(directory)
    fake_ssh = temp / 'ssh'
    fake_ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$LAUNCHER_SSH_LOG"\n'
                        'exit "$LAUNCHER_SSH_EXIT"\n')
    fake_ssh.chmod(0o755)
    log = temp / 'ssh-args.txt'
    for name, mode in [('test-eye.command', '--demo --amplitude 12 --period 20 --seconds 120'),
                       ('manual-eye.command', '--manual')]:
        launcher = ROOT / name
        assert os.access(launcher, os.X_OK), 'Finder launcher must be executable'
        subprocess.run(['/bin/bash', '-n', str(launcher)], check=True)
        args = expected_args[:-1] + [expected_args[-1] + mode]
        for code in (0, 23, 255):
            log.unlink(missing_ok=True)
            env = dict(os.environ, PATH=str(temp) + os.pathsep + os.environ.get('PATH', ''),
                       LAUNCHER_SSH_LOG=str(log), LAUNCHER_SSH_EXIT=str(code))
            result = subprocess.run([str(launcher)], cwd=temp, env=env, input='',
                                    capture_output=True, text=True, timeout=3)
            assert result.returncode == code, result
            assert log.read_text().splitlines() == args, 'Wrong arguments or unexpected retry'
            assert '按回车' not in result.stdout, 'Non-interactive execution must not wait for input'
            if code == 0:
                assert '客户端已发送居中指令' in result.stdout
            else:
                assert f'退出码 {code}' in result.stderr

    launcher = ROOT / 'test-dual-eye.command'
    assert os.access(launcher, os.X_OK)
    subprocess.run(['/bin/bash', '-n', str(launcher)], check=True)
    control = expected_args[-1].rstrip()
    remotes = {
        '1': f'{control} --xy 0 0 && {control} --screen 1 --demo --amplitude 12 --period 20 --seconds 60',
        '2': f'{control} --xy 0 0 && {control} --screen 2 --demo --amplitude 12 --period 20 --seconds 60',
        '3': f'{control} --xy 0 0 && {control} --screen both --demo --amplitude 12 --period 20 --seconds 180',
        '4': f'{control} --manual',
        '5': f'{control} --xy 0 0',
    }
    for code in (0, 23, 255):
        log.unlink(missing_ok=True)
        env = dict(os.environ, PATH=str(temp) + os.pathsep + os.environ.get('PATH', ''),
                   LAUNCHER_SSH_LOG=str(log), LAUNCHER_SSH_EXIT=str(code))
        result = subprocess.run([str(launcher)], cwd=temp, env=env,
                                input='wrong\n1\n2\n3\n4\n5\n1\nQ\n',
                                capture_output=True, text=True, timeout=3)
        assert result.returncode == code, result
        expected = []
        for choice in ('1', '2', '3', '4', '5', '1'):
            expected.extend(expected_args[:-1] + [remotes[choice]])
        assert log.read_text().splitlines() == expected, 'Wrong screen, automatic retry or failed replay'
        assert '请输入' in result.stdout
        if code:
            assert result.stderr.count(f'退出码 {code}') == 6
            assert '本轮结束' not in result.stdout
        else:
            assert result.stdout.count('本轮结束') == 6
    for input_text in ('', 'Q\n'):
        log.unlink(missing_ok=True)
        result = subprocess.run([str(launcher)], cwd=temp, env=env, input=input_text,
                                capture_output=True, text=True, timeout=3)
        assert result.returncode == 0 and not log.exists(), 'Menu must wait for explicit selection'
print('PASS launcher: shell syntax, executable bit, SSH/PTY arguments, exit codes, no retries or non-interactive pause')
print('PASS dual test menu: single-eye/both/manual/static choices, replay, invalid input, EOF and failure recovery menu')
