# Alia Eye · 双屏眼睛模块

供上位机器人项目接入的 2D 眼睛显示模块。树莓派通过 Wi-Fi 控制一块经典 ESP32，ESP32 独立驱动两块 ST7789S TFT。图像驻留 ESP32 Flash，上位程序只发送位置、校准或背光指令；设备上电即可显示。

```text
上位程序 / 树莓派 Python
         │ Wi-Fi TCP :4242
         ▼
       ESP32 ── VSPI → 屏幕 1
         └──── HSPI → 屏幕 2

可选工具：本机校准 WebUI → 同一套 TCP 或 USB 协议
每屏：720×640 虚拟画布 → 240×320 Viewport 裁切
```

## 交付入口

| 需求 | 入口 |
|---|---|
| 上位程序接入、坐标、协议、连接管理、参数保存 | [上位项目接入说明](docs/INTEGRATION.md) |
| 配网、接线、构建烧录、界面使用、历史验证与排障 | [操作与验证记录](docs/OPERATIONS.md) |
| Python 控制接口 / 命令行 | [pi/eye_control.py](pi/eye_control.py) |
| 校准、亮度及保存接口 / WebUI 服务 | [pi/calibrate.py](pi/calibrate.py) |
| ESP32 固件及引脚配置 | [firmware/](firmware/)、[display_setup.h](firmware/include/display_setup.h) |
| 已发布的 v10 固件与 SHA256 | [GitHub v10 Release](https://github.com/Ke-Qiao/screen_eye_Alia/releases/tag/v10) |

交付整理保持 v10 的运行代码、协议、图像与驱动不变。交付 ZIP 额外带有 `prebuilt/` 应用固件、`HANDOFF.json` 文件清单和包外的 `SHA256SUMS.txt`；源码仓库不存放凭据或设备 NVS 备份。

## 硬件与版本基线

| 项目 | 当前值 |
|---|---|
| 固件标识 | `alia-eye-wifi-10` |
| 板卡 | 经典 ESP32 DevKit，已验证 ESP32-D0WD-V3 / 4 MB Flash；不适用 S3/C3 |
| 两块屏幕 | ST7789S，240×320，竖装，屏幕 1 排针在上、屏幕 2 在下；无 RST/RES，无 MISO/TE 接线 |
| SPI | 屏幕 1：VSPI；屏幕 2：HSPI；10 MHz / Mode 3，顺序阻塞更新 |
| 供电 | 两屏使用独立 3.3V 电源，并与 ESP32 共地；BL 高电平点亮 |
| 图案 | 原图正向，150×228；白色外部背景，保留已验收的眼睛内部颜色 |
| 校准 | 每屏独立中心、0.1° 旋转、上下定义；仅显式保存后持久化 |
| 背光 | 每屏 0–100% 整数 PWM，5 kHz / 10-bit，出厂默认 8% |
| 运行依赖 | Python 3.10+、`pyserial==3.5`；校准页面由标准库 HTTP 服务提供 |

屏幕编号对应接线，不预设左眼或右眼，由上位项目映射。软件只做平面渲染与裁切，不包含 3D 球面或透镜畸变校正。

## 在已配网设备上快速验证

在树莓派或 macOS/Linux 主机的项目根目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install pyserial==3.5
```

从设备部署方取得现有控制令牌，放在当前用户拥有、权限为 `0600` 的普通文件中。以下地址和路径是占位符，需替换为目标设备的实际值；新设备先按[配网说明](docs/OPERATIONS.md#新设备配网与树莓派部署)配置。

```sh
export EYE_HOST='ESP32 的实际 IP'
export EYE_TOKEN_FILE='/私有目录/eye-control.token'
chmod 600 "$EYE_TOKEN_FILE"

# 仅将屏幕 1 的相对眼动目标设为向上 12 px；不保存校准。
.venv/bin/python pi/eye_control.py \
  --host "$EYE_HOST" --token-file "$EYE_TOKEN_FILE" --screen 1 --xy 0 -12

# 双眼回到各自的校准中心。
.venv/bin/python pi/eye_control.py \
  --host "$EYE_HOST" --token-file "$EYE_TOKEN_FILE" --xy 0 0
```

`OKXY` 只确认接受目标，设备随后平滑移动。ESP32 同时只处理一个 TCP 控制连接，测试前暂停上位控制程序和自动演示。需要校准时运行：

```sh
.venv/bin/python pi/calibrate.py \
  --host "$EYE_HOST" --token-file "$EYE_TOKEN_FILE"
```

打开 <http://127.0.0.1:8080/>。位置、角度、上下与亮度实时应用，点击“保存到设备”才写入 NVS。调整结束后关闭服务并将控制权交回上位程序。macOS 的四个 `.command` 脚本保留原部署环境的地址与路径，新环境优先使用以上参数化命令。

## 开发检查

运行主机检查和固件构建时安装完整依赖；另需 C++17 编译器、Node.js：

```sh
.venv/bin/pip install -r requirements.txt
.venv/bin/python tests/check.py
.venv/bin/python tests/test_provision.py
.venv/bin/python tests/test_launcher.py
.venv/bin/python tests/test_manual.py
.venv/bin/python tests/test_calibration_web.py
.venv/bin/python tests/test_integration_examples.py
node tests/test_calibration_live.js
export PLATFORMIO_CORE_DIR="$PWD/.platformio"
.venv/bin/pio run -d firmware -e esp32dev
```

固件平台固定为 espressif32 6.9.0 / Arduino-ESP32 2.0.17。图像已生成，无需在接入时重新转换。烧录、应用分区 `0x10000` 和恢复步骤见[操作说明](docs/OPERATIONS.md#构建和烧录)，不能把应用固件写到 `0x0`。

## 已验证范围与交付边界

- v9 的实时跟随、两屏颜色、白底和边缘已获用户验收；v10 在此基础上增加独立背光，渲染路径不变。
- 2026-10-17 已验证保存后 EN 硬件重启，以及一次 ESP32 与两屏全部拔电至少 10 秒的真实冷启动。非默认的亮度 17% / 29%、中心、角度和上下均自行恢复；用户确认两屏正常。
- 冷启动验证结束后，两屏背光均恢复并保存为 8%；当前装配的校准值见[接入说明](docs/INTEGRATION.md#当前装配与迁移)。这些值不应直接作为新装配的出厂参数。
- 未接入 TE，`SYNC` 只确认一轮绘制和 SPI 发送，不能保证垂直同步。历史彩色横条/丢图和偶发无线超时的根因尚未完全定位，长期运动稳定性仍需上位项目在最终装配中验收。

更多证据及各阶段结论见[历史验证记录](docs/OPERATIONS.md#测试与已验证状态)。第三方 ST7789 初始化来源与许可保留在 [firmware/NOTICE](firmware/NOTICE)。
