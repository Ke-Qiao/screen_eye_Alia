#!/usr/bin/env python3
"""Execute the handoff's Python examples against the real host parsers, without hardware."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import re
import sys
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXAMPLES = re.findall(r'```python\n(.*?)\n```', (ROOT / 'docs/INTEGRATION.md').read_text(), re.S)
assert len(EXAMPLES) == 2
STATUS = b'STATUS alia-eye-wifi-10 aa:bb:cc:dd:ee:ff 1 0 0\n'
GEOMETRY = b'INFO2 240 320 -285 285 -206 206\n'
CALIBRATION = [b'CAL 1 120 160 0 0 0 0\n', b'CAL 2 120 160 0 1 0 0\n']
BACKLIGHT = [b'BL 1 8 82\n', b'BL 2 8 82\n']
TOKEN = '1' * 64  # Fake test credential, never sent to hardware.


def run_example(index, module, replies):
    link = MagicMock()
    link.__enter__.return_value = link
    link.write.side_effect = len
    link.read_until.side_effect = replies
    output, namespace = StringIO(), {}
    with patch(module + '.TcpConnection', return_value=link) as connect, \
            patch(module + '.read_token', return_value=TOKEN) as read, redirect_stdout(output):
        try:
            exec(compile(EXAMPLES[index], 'docs/INTEGRATION.md', 'exec'), namespace)
        finally:
            connect.assert_called_once_with('ESP32 的实际 IP')
            read.assert_called_once_with('/私有目录/eye-control.token')
            link.authenticate.assert_called_once_with(TOKEN)
    return link, namespace


link, example = run_example(0, 'pi.eye_control', [
    STATUS, b'EYES 2\n', GEOMETRY, b'OKXY 1 0 -12\n', b'OKXY 2 12 0\n',
    b'OKSYNC\n', b'POS 1 0 -12 0 -4\n',
])
assert link.write.call_args_list == [call(packet) for packet in (
    b'STATUS\n', b'EYES\n', b'INFO2\n', b'EYE 1 XY 0 -12\n',
    b'EYE 2 XY 12 0\n', b'SYNC\n', b'EYE 1 POS\n',
)]
assert example['position'] == ['POS', '0', '-12', '0', '-4']
assert link.read_until.call_count == 7
link.__exit__.assert_called_once()

try:
    run_example(0, 'pi.eye_control', [STATUS.replace(b' 1 0 0', b' 0 0 0')])
except RuntimeError as error:
    assert '目标不是启用显示的 v10 固件' in str(error)
else:
    raise AssertionError('Example must reject a diagnostic firmware before sending movement')

link, example = run_example(1, 'pi.calibrate', [
    STATUS, GEOMETRY, *CALIBRATION, *BACKLIGHT,
    STATUS, GEOMETRY, b'OKBL 1 8\n', *CALIBRATION, *BACKLIGHT,
])
assert link.write.call_args_list == [call(packet) for packet in (
    b'STATUS\nINFO2\nEYE 1 CAL\nEYE 2 CAL\n',
    b'EYE 1 BL\nEYE 2 BL\n', b'STATUS\nINFO2\n',
    b'EYE 1 BL 8\nEYE 1 CAL\nEYE 2 CAL\nEYE 1 BL\nEYE 2 BL\n',
)]
assert [eye['brightness'] for eye in example['state']['eyes']] == [8, 8]
assert not any(eye['unsaved'] for eye in example['state']['eyes'])
assert link.read_until.call_count == 13
link.close.assert_called_once()
print('PASS handoff examples: real host parsers, per-eye movement, version guard, backlight, no save/replay, connection release (offline mocks)')
