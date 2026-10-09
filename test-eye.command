#!/bin/bash

printf '%s\n' \
  '通过树莓派运行眼睛上下往返演示：2 分钟，幅度 60 px，周期 4 秒。' \
  '按 Ctrl+C 可提前停止并发送居中指令。' \
  '背光 8%、图案 325×214 像素来自已烧录的固件配置；本脚本只运行运动演示。'

ssh -tt -o BatchMode=yes -o ConnectTimeout=10 ke-qiao@192.168.10.221 \
  'python3 -u /home/ke-qiao/Alia-eye/pi/eye_control.py --host 192.168.10.133 --token-file /home/ke-qiao/Alia-eye/eye-control.token --demo --amplitude 60 --period 4 --seconds 120'
ssh_status=$?

if [ "$ssh_status" -eq 0 ]; then
  printf '%s\n' '演示已结束，客户端已发送居中指令。'
else
  printf '运行失败（退出码 %s），请查看上方错误。\n' "$ssh_status" >&2
fi

if [ -t 0 ] && [ -t 1 ]; then
  read -r -p '按回车退出。' launcher_reply
fi
exit "$ssh_status"
