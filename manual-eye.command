#!/bin/bash

printf '%s\n' \
  '通过树莓派手动控制眼睛：方向键或 WASD 每次移动 10 px，空格居中。' \
  'R 同时反转上下和左右；Q 或 Ctrl+C 居中后退出。'

ssh -tt -o BatchMode=yes -o ConnectTimeout=10 ke-qiao@192.168.10.221 \
  'python3 -u /home/ke-qiao/Alia-eye/pi/eye_control.py --host 192.168.10.133 --token-file /home/ke-qiao/Alia-eye/eye-control.token --manual'
ssh_status=$?

if [ "$ssh_status" -eq 0 ]; then
  printf '%s\n' '手动控制已结束，客户端已发送居中指令。'
else
  printf '运行失败（退出码 %s），请查看上方错误。\n' "$ssh_status" >&2
fi

if [ -t 0 ] && [ -t 1 ]; then
  read -r -p '按回车退出。' launcher_reply
fi
exit "$ssh_status"
