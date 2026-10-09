# Alia Eye · 树莓派无线控制 ESP32 仿生眼睛

用于实体机器人眼睛模块的 2D 渲染系统。树莓派通过 Wi-Fi 发送位置指令，ESP32 在大虚拟画布上移动眼睛图案，经固定 Viewport 裁切后输出到 ST7789S TFT。

```text
电脑键盘 / .command 脚本 → SSH → 树莓派 → Wi-Fi TCP :4242 → ESP32
                                                           ↓
                                   720×640 虚拟画布 → 240×320 Viewport → SPI TFT
```

图片驻留 ESP32 Flash，树莓派只发送控制指令。无需网页、3D 球面或 PSRAM；物理透镜不参与软件计算，不做畸变校正或 LUT。

## 当前版本

| 项目 | 配置 |
|---|---|
| 固件标识 | `alia-eye-wifi-3` |
| 板卡 | 经典 ESP32 DevKit，已在 ESP32-D0WD-V3 / 4 MB Flash 验证 |
| 显示屏 | ST7789S，原生 240×320，实物横向安装 |
| 图案 | 用户提供的蓝色眼睛，顺时针旋转 90°，325×214 像素 |
| 背光 | GPIO25，高电平有效，5 kHz / 10-bit PWM，8%（82/1024） |
| 渲染 | 720×640 画布，240×320 固定窗口，8 行分块，缓冲 3,840 字节 |
| 运动 | 双轴平滑移动，160 px/s/轴，最高调度频率约 30 Hz |
| 控制 | 方向键/WASD、单次二维定位、上下往返演示；兼容 USB 串口 |

![当前眼睛纹理](assets/eye-texture.png)

上图是软件纹理，不是实机照片。尺寸在 428×282 的基础上先缩小约 20%，再缩小约 5%，按整数像素取整。8% 指 PWM 占空比，不是测得的光学亮度比例。

## 目录

```text
firmware/                 PlatformIO 工程、ESP32 固件和 RGB565 数据
pi/eye_control.py         树莓派 TCP/串口客户端与键盘控制
tools/prepare_image.py    原图裁切、缩放、旋转及 RGB565 转换
tools/provision_wifi.py   USB 首次配网与私有控制令牌生成
assets/                   原始图片与当前纹理
tests/                    渲染、协议、配网、键盘和启动脚本测试
manual-eye.command        macOS 双击启动手动控制
test-eye.command          macOS 双击启动 2 分钟演示
```

仓库包含构建所需的图像资源。虚拟环境、依赖缓存、日志、控制令牌、Wi-Fi 凭据和设备 Flash 备份不随源码发布。

## 已部署设备：直接使用

在 Finder 中双击 `manual-eye.command`。先退出正在运行的自动演示，ESP32 一次只接受一个控制连接。

| 按键 | 操作 |
|---|---|
| ↑ / W | 向上移动 10 px |
| ↓ / S | 向下移动 10 px |
| ← / A | 向左移动 10 px |
| → / D | 向右移动 10 px |
| 空格 | 两轴居中 |
| R | 同时反转上下、左右方向，仅本次会话有效 |
| Q / Ctrl+C | 发送居中指令并退出 |

按住按键可通过键盘连发持续移动，终端显示目标坐标。启动时先居中，空闲时每 5 秒发送查询保活；Ctrl+C 等待在途回复完成后再发送居中指令。结束后按回车关闭窗口。

`test-eye.command` 运行 120 秒自动演示，幅度 60 px、周期 4 秒；Ctrl+C 可提前结束并居中。

两个启动脚本保留当前设备的连接设置：

| 设置 | 当前值 |
|---|---|
| 树莓派 SSH | `ke-qiao@192.168.10.221` |
| 树莓派程序目录 | `/home/ke-qiao/Alia-eye` |
| ESP32 地址 | `192.168.10.133`，TCP 4242 |
| 树莓派令牌文件 | `/home/ke-qiao/Alia-eye/eye-control.token`，权限 0600 |

更换网络或设备时，修改两个 `.command` 文件中的 SSH 目标、ESP32 地址、远端程序及令牌路径。脚本复用 SSH 认证，不含密码或令牌内容。新环境使用 Python 虚拟环境时，也需将远端 `python3` 改为相应解释器的绝对路径。

## 硬件接线

本接线用于经典 ESP32，与 ESP32-S3/C3 不通用。

| TFT 信号 | ESP32 / 接法 |
|---|---|
| SCLK / SCL | GPIO18 |
| MOSI / SDA | GPIO23 |
| MISO / SDO | GPIO19（像素输出不依赖读回） |
| CS | GPIO5 |
| DC | GPIO27 |
| RST / RES | 模块无此引脚，`TFT_RST=-1`，使用软件复位 |
| BL | GPIO25，高电平点亮 |
| VCC | 3V3 |
| GND | 共地 |

配置入口为 [`firmware/include/display_setup.h`](firmware/include/display_setup.h)。驱动使用 TFT_eSPI 的 `ST7789_DRIVER`，`setRotation(0)` 保持原生 240×320 寻址。背光由应用在首帧绘制后开启，TFT_eSPI 不接管背光初始化。

## 构建和烧录

使用 Python 3.10+、Git，以及测试时需要的 C++ 编译器。Python 依赖在 [`requirements.txt`](requirements.txt) 中指定，板卡平台与 TFT_eSPI 版本在 [`firmware/platformio.ini`](firmware/platformio.ini) 中指定。

```sh
git clone https://github.com/Ke-Qiao/screen_eye_Alia.git
cd screen_eye_Alia
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export PLATFORMIO_CORE_DIR="$PWD/.platformio"
.venv/bin/pio run -d firmware -e esp32dev
```

仓库包含生成后的图案，只改控制逻辑无需重新转换。更换尺寸或原图时运行：

```sh
.venv/bin/python tools/prepare_image.py
```

默认裁切原图 `(512,120)–(1024,896)`，旋转前缩放上限为 492×325，再顺时针旋转 90°，输出 PNG、RGB565 数据和尺寸头文件。`--max-size` 指旋转前尺寸上限；`--rotation 0` 保留原方向。修改引脚配置后先清理再构建：

```sh
.venv/bin/pio run -d firmware -e esp32dev -t clean
.venv/bin/pio run -d firmware -e esp32dev
```

烧录前确认端口与目标板；以下是当前 macOS 串口示例，其他机器需替换：

```sh
.venv/bin/pio device list
.venv/bin/pio run -d firmware -e esp32dev -t upload --upload-port /dev/cu.usbserial-0001
```

常规上传保留 NVS 网络配置。`esp32dev-wireless` 是禁用 TFT GPIO 的无线诊断构建，仅生成软件像素，不代表屏幕出图。

## 新设备配网与树莓派部署

ESP32 需要 2.4 GHz Wi-Fi；树莓派可以使用同一路由器的 5 GHz，只要两台设备能够互访。配网在连接 ESP32 的电脑上通过 USB 串口完成，密码由隐藏提示输入：

```sh
mkdir -p .build/private
chmod 700 .build/private
.venv/bin/python tools/provision_wifi.py \
  --port /dev/cu.usbserial-0001 --ssid '你的 2.4 GHz Wi-Fi 名称' \
  --token-file .build/private/eye-control.token \
  --expect-mac aa:bb:cc:dd:ee:ff
```

将示例 MAC 和端口替换为目标设备实际值。工具先核对身份再保存配置；`OK CONFIG` 表示配置已保存，`NET <IP> 4242` 表示联网成功。已有有效令牌不会覆盖。也可用 `--wifi-file` 读取仅含 `ssid`、`password` 的 JSON，须为当前用户拥有、权限 0600 的普通文件，不应进入 Git。

在树莓派上准备客户端，目录须与启动脚本一致：

```sh
git clone https://github.com/Ke-Qiao/screen_eye_Alia.git ~/Alia-eye
cd ~/Alia-eye
python3 -m venv .venv
.venv/bin/pip install pyserial==3.5
```

树莓派仅需 Python、pySerial 和标准库 curses。将电脑生成的 `.build/private/eye-control.token` 通过 SSH/SCP 私密复制到树莓派工程目录的 `eye-control.token`，设为该用户拥有且权限 0600。文件内令牌须与设备 NVS 一致。

在树莓派工程目录执行，替换为配网工具报告的 ESP32 地址：

```sh
.venv/bin/python pi/eye_control.py \
  --host 192.168.10.133 --token-file eye-control.token --manual
.venv/bin/python pi/eye_control.py \
  --host 192.168.10.133 --token-file eye-control.token --xy 30 -20
.venv/bin/python pi/eye_control.py \
  --host 192.168.10.133 --token-file eye-control.token \
  --demo --amplitude 60 --period 4 --seconds 120
```

USB 控制可用 `--port /dev/ttyUSB0` 替代 `--host` 和 `--token-file`。无线运行时 USB 线可以继续供电，应关闭串口监视器，避免打开串口引入复位干扰。DHCP 地址变化后更新脚本，或在路由器保留地址。

## 渲染坐标与控制协议

大画布只是坐标空间，不分配整张画布。固定窗口左上角为 `(240,160)`，移动的是图案，每帧覆盖整个窗口以清除旧位置。

屏幕横向安装：默认视觉向右对应面板 Y 减小，视觉向下对应面板 X 增大；图案保持既有 90° 方向。`x` 范围 −213…+213，`y` 范围 −197…+198，分别钳制。如果安装方向相反，手动模式用 R 反转两轴按键方向。

USB 为 115200 baud / 8N1；命令为 ASCII、换行结束。TCP 端口 4242，先发 `AUTH <64位小写hex令牌>`，收到 `OK AUTH` 后使用以下指令：

| 请求 | 响应 | 含义 |
|---|---|---|
| `INFO2` | `INFO2 240 320 -213 213 -197 198` | 屏幕尺寸与两轴范围 |
| `XY 30 -20` | `OKXY 30 -20` | 设置两轴目标 |
| `POS` | `POS <targetX> <targetY> <drawnX> <drawnY>` | 目标与当前渲染位置 |
| `INFO` | `INFO 240 320 -197 198` | 兼容旧单轴客户端 |
| `Y -60` | `OK -60` | 只修改 y，保留 x |
| `STATUS` | `STATUS alia-eye-wifi-3 <MAC> <display> <targetY> <drawnY>` | 版本、身份、显示状态及 y 位置 |
| 非法或过长输入 | `ERR command` / `ERR line` | 拒绝输入，不改目标 |

`OK`/`OKXY` 表示目标已接受；用 `POS` 核对是否已绘制到目标。Python 接口为 `EyeController.connect_2d()` / `set_xy(x, y)`，旧 `connect()` / `set_y()` 保留兼容。

未认证连接 3 秒关闭，已认证但空闲 12 秒关闭。手动模式每 5 秒保活。断线后设备保持最近目标并尝试重连，客户端报错退出，不重发结果未知的指令；断网时无法保证退出居中。协议有令牌认证，无 TLS，用于受信任的私人局域网。

## 测试与已验证状态

在 macOS/Linux 开发环境的项目根目录执行：

```sh
.venv/bin/python tests/check.py
.venv/bin/python tests/test_provision.py
.venv/bin/python tests/test_launcher.py
.venv/bin/python tests/test_manual.py
```

覆盖双轴分块渲染与完整画布裁切的逐像素一致性、图案尺寸与旋转、RGB565、两轴边界、指令解析、配网身份检查、私有文件权限、真实 PTY/回环 TCP、方向键、心跳、待收回复时 Ctrl+C、错误退出和终端恢复。生成的 `.build/*.png` 是软件预览，不是实机照片。

2026-10-09 已在当前设备完成：

- 显示版和无线诊断版构建通过；显示版烧录及 Flash hash 校验通过。
- 启动读回 `BACKLIGHT 8 82 5000`、`INFO2 240 320 -213 213 -197 198`。
- 树莓派无线验证四方向、对角移动、边界钳制、非法指令及旧 Y 指令兼容。
- 本机手动脚本实测方向键、WASD、R、空格、超过 12 秒空闲后的继续控制及 Ctrl+C；独立重连确认 `POS 0 0 0 0`。
- 用户已目视确认图案旋转方向与横向安装后的上下运动轴；四方向正负关系和新尺寸观感仍由用户现场确认。

长时间稳定性和光学亮度未测量。历史联调曾出现控制超时，根因尚未确定。详细日志、历史说明和设备恢复备份留在开发机 `.build/`，未上传；GitHub 源码不含现场凭据，新设备需独立配网。
