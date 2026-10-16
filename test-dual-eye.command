#!/bin/bash

control='python3 -u /home/ke-qiao/Alia-eye/pi/eye_control.py --host 192.168.10.133 --token-file /home/ke-qiao/Alia-eye/eye-control.token'
ssh_status=0
printf '%s\n' \
  'Alia 双屏测试：准备好观察屏幕后，再选择测试项。' \
  '屏幕编号对应接线图。背光 8%；当前为竖装双屏，图案正向、上下轴对应屏幕竖直方向，幅度 12 px，往返周期 20 秒。' \
  '先退出其他控制窗口；运动中按 Ctrl+C 停止并回中，随后可重新选择。'

while true; do
  printf '\n%s\n' \
    '1  只让屏幕 1 上下移动（60 秒），屏幕 2 保持居中' \
    '2  只让屏幕 2 上下移动（60 秒），屏幕 1 保持居中' \
    '3  双屏同步上下移动（3 分钟）' \
    '4  方向键手动测试：1/2/0 选屏，方向键或 WASD 移动，空格居中，Q 返回' \
    '5  双屏居中，保持静止观察颜色和横条' \
    'Q  退出（保留屏幕当前画面）'
  if ! read -r -p '选择 [1-5/Q]：' choice; then break; fi
  case "$choice" in
    1|2) remote="$control --xy 0 0 && $control --screen $choice --demo --amplitude 12 --period 20 --seconds 60" ;;
    3) remote="$control --xy 0 0 && $control --screen both --demo --amplitude 12 --period 20 --seconds 180" ;;
    4) remote="$control --manual" ;;
    5) remote="$control --xy 0 0" ;;
    q|Q) break ;;
    *) printf '%s\n' '请输入 1、2、3、4、5 或 Q。'; continue ;;
  esac

  ssh -tt -o BatchMode=yes -o ConnectTimeout=10 ke-qiao@192.168.10.221 "$remote"
  ssh_status=$?
  if [ "$ssh_status" -eq 0 ]; then
    printf '%s\n' '本轮结束，客户端已发送居中指令。可保持静止观察，或重新选择测试项。'
  else
    printf '测试中断（退出码 %s）。请保留上方错误；当前是否回中尚未确认。\n' "$ssh_status" >&2
    printf '%s\n' '返回菜单后，可选择 5 用新连接请求回中，或选择 Q 退出。' >&2
  fi
done
exit "$ssh_status"
