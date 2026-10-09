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
    for name, mode in [('test-eye.command', '--demo --amplitude 60 --period 4 --seconds 120'),
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
print('PASS launcher: shell syntax, executable bit, SSH/PTY arguments, exit codes, no retries or non-interactive pause')
