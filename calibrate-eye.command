#!/bin/bash
set -u
project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_dir" || exit 1
printf '%s\n' 'Alia 眼睛校准：http://127.0.0.1:8080/' '请退出其他串口控制窗口。按 Ctrl+C 关闭界面服务。'
"$project_dir/.venv/bin/python" "$project_dir/pi/calibrate.py" --port /dev/cu.usbserial-0001
result=$?
if [ -t 0 ] && [ -t 1 ]; then read -r -p '按回车退出。' reply; fi
exit "$result"
