# 上位项目接入说明 · v10

本文对应 `alia-eye-wifi-10`，面向持有目标 ESP32 控制令牌的上位程序。固件、驱动、纹理与已验收 v10 保持一致。操作与构建细节见 [OPERATIONS.md](OPERATIONS.md)。

## 模块边界与入口

ESP32 负责两只眼的渲染、目标追踪、安装补偿和背光，上位程序负责业务动作、左/右眼映射及连接调度。Wi-Fi 控制端口为 TCP `4242`；USB 串口 `115200 / 8N1` 用于首次配网、维护或有线控制。设备不依赖上位程序、网页或网络连接就能显示启动画面。

| 文件 | 接入方式 |
|---|---|
| `pi/eye_control.py` | 导入 `TcpConnection`、`EyeController`、`read_token`；已有 CLI 可用于冒烟测试 |
| `pi/calibrate.py` | `Device.execute()` 提供经过校验的校准、背光及保存操作；`main()` 才会启动 HTTP 服务 |
| `pi/calibration.html` | 人工安装校准界面，非运行时必需 |
| `firmware/include/display_setup.h` | 已确认的引脚、默认安装方向、时钟、画布与背光默认值 |

当前按源码目录接入：将本项目作为上位项目的子目录或独立 checkout，并将**本项目根目录**加入 Python 模块搜索路径，例如设置 `PYTHONPATH=/opt/screen_eye_Alia`，再导入 `pi.eye_control`。不依赖当前工作目录，令牌使用绝对路径；不要将 `.venv`、`.platformio` 或开发机 `.build` 复制进上位源码。运行只需 Python 3.10+ 和 `pyserial==3.5`，构建工具与 Pillow 仅在开发时安装。

## Python 眼动接入

下例中的地址和令牌路径均由上位项目配置提供。在同一个工作线程中顺序使用连接，不要让多线程共享 `EyeController` 的请求与回复。

```python
from pi.eye_control import EyeController, TcpConnection, read_token

host = "ESP32 的实际 IP"
token_file = "/私有目录/eye-control.token"
token = read_token(token_file)

with TcpConnection(host) as link:
    link.authenticate(token)
    eye = EyeController(link)
    status = eye.request("STATUS", "STATUS")
    if len(status) != 6 or status[1] != "alia-eye-wifi-10" or status[3] != "1":
        raise RuntimeError("目标不是启用显示的 v10 固件")
    # status[2] 是设备 MAC，可与上位项目配置中的目标身份核对。
    eye.connect_eyes()
    width, height, min_x, max_x, min_y, max_y = eye.connect_2d()

    eye.set_xy(0, -12, screen=1)   # 屏幕 1 向上。
    eye.set_xy(12, 0, screen=2)    # 屏幕 2 向右；屏幕 1 保持目标。
    eye.request("SYNC", "OKSYNC") # 等待本轮绘制/发送，不是到达目标。
    position = eye.request_eye("POS", "POS", screen=1)
    print(position)               # ["POS", targetX, targetY, drawnX, drawnY]，元素为字符串。
```

关闭连接不会回中；设备会继续追踪最近目标。需要回中时由上位程序明确调用 `eye.set_xy(0, 0)`，这会控制双眼。需要等待到达时，在有截止时间的循环中查询各屏 `POS`，比较 `targetX/Y` 与 `drawnX/Y`；查询间隔可从 100 ms 起，不要忙轮询。`SYNC` 不能替代到达判断，`drawn` 也只代表软件绘制完成，无法证明面板实际出图。

| 现有方法 | 返回/行为 |
|---|---|
| `TcpConnection(host, port=4242)` | 建立连接；支持 `with` 释放 |
| `link.authenticate(read_token(path))` | 认证；每次新 TCP 连接都需要 |
| `eye.connect_eyes()` | 检查双屏能力 |
| `eye.connect_2d()` | 返回 `(width, height, min_x, max_x, min_y, max_y)` 并初始化位置范围 |
| `eye.set_xy(x, y, screen=1)` | 返回钳制后的 `(x, y)`；必须先 `connect_2d()` |
| `eye.set_y(y, screen=2)` | 保留该屏 x，返回钳制后的 y；先 `connect()` 或 `connect_2d()` |
| `eye.request_eye("POS", "POS", screen=1)` | 校验并去掉回复中的屏幕编号 |
| `eye.request(command, prefix)` | 扩展指令；仅检查回复前缀，额外字段由调用方校验 |

`screen` 只接受整数 `1` / `2`；省略或传 `None` 表示双眼。位置必须为 32 位有符号整数，不能传浮点或布尔值。`request_many()` 一批请求共用 5 秒回复截止时间，多条命令的发送内容不得超过 128 字节；普通上位接入优先使用顺序方法，避免自行堆积请求。

## 坐标与安装方向

| 参数 | 单位、范围与含义 |
|---|---|
| 屏幕编号 | 1 / 2 对应接线图，不预设左右眼 |
| 眼动 `x, y` | 相对于**各自校准中心**的像素偏移；右为正 x，下为正 y；`(0,0)` 回到校准中心 |
| 眼动范围 | 当前 `x=-285…285`，`y=-206…206`；运行时通过 `INFO2` 读取，不写死；超出范围会钳制 |
| 校准中心 | 屏幕逻辑坐标，`center_x=0…239`、`center_y=0…319`，默认 `(120,160)` |
| 校准角度 | −180…180°，精度 0.1°，正值顺时针；线协议使用整数 `angle10`，例如 3.5° → 35 |
| 屏幕上方 | `connector` 排针端在上；`opposite` 排针对侧在上；同时补偿图案和运动坐标 |
| 背光 | 整数 0…100%，0 关闭、100 常亮；PWM 占空比，不是测量的光学亮度 |

眼动范围覆盖虚拟画布，允许图案部分或完全离开 Viewport。最终“图案可见范围”由当前中心、旋转和安装决定，上位项目可按产品表现限制目标。旋转补偿围绕图案中心进行，不改变 x/y 表示屏幕左右上下的定义。

## 校准、背光与显式保存

复用校准服务已经校验过的入口，无需启动网页。下例以单独的校准操作阶段运行，先释放其他控制连接：

```python
from pi.calibrate import Device

device = Device(host="ESP32 的实际 IP", token_file="/私有目录/eye-control.token")
try:
    state = device.execute("/api/state")  # 只读；含两屏的当前值与 unsaved。
    # 需要调整时才调用下列操作：
    state = device.execute("/api/backlight", {"screen": 1, "brightness": 8})
    # 安装校准示例，请使用实际测定值：
    # state = device.execute("/api/calibration", {
    #     "screen": 1, "center_x": 120, "center_y": 160,
    #     "angle": 0.0, "top": "connector", "brightness": 8,
    # })
    # 用户明确要求保存时：
    # state = device.execute("/api/save", {})
finally:
    device.close()
```

状态返回结构：

```json
{
  "firmware": "alia-eye-wifi-10", "mac": "目标设备 MAC", "display": true,
  "width": 240, "height": 320,
  "eyes": [
    {"screen": 1, "center_x": 120, "center_y": 160, "angle": 0.0,
     "top": "connector", "brightness": 8, "unsaved": false},
    {"screen": 2, "center_x": 120, "center_y": 160, "angle": 0.0,
     "top": "opposite", "brightness": 8, "unsaved": false}
  ]
}
```

上例 JSON 是默认值示例，不是当前设备的测量结果。`/api/calibration` 的 `brightness` 可省略，省略时保持该屏亮度。`/api/backlight` 只改亮度；`/api/calibration` 会先将所选眼睛的相对眼动目标设为 `(0,0)`；`/api/reset` 接收 `{"screen":1}`，将该屏校准和背光恢复编译默认值。几何操作等到本轮 `SYNC` 完成后返回，亮度调整不触发像素重画。

`/api/save` 接收空对象，保存**两屏**当前校准与背光，并非只保存界面当前选中的屏幕。网络、校准和背光的持久化边界如下：

| 配置 | 存放位置 | 何时写入/启动行为 |
|---|---|---|
| Wi-Fi 与令牌 | NVS `eye_net/config` | USB 配网时保存；启动自动联网 |
| 两屏校准 | NVS `eye_cal/config` | `SAVECAL` / 保存按钮；有效配置启动加载，无有效配置则用编译默认 |
| 两屏亮度 | NVS `eye_cal/backlight` | 同一次显式保存；无有效配置默认各 8% |
| 相对眼动目标 | RAM | 不持久化；启动为 `(0,0)` |

仅修改参数不写 Flash；`unsaved` 表示该屏校准或亮度尚未保存。`ERR storage` 可能表示几何已写入、背光未写入，保存不是跨两个 NVS 项的原子事务；错误后读取并核对两屏状态，由用户决定是否重新保存。普通眼动不能顺手触发永久保存。

`Device` 也只能由一个线程顺序使用；持续调整时复用认证连接，空闲 0.75 秒后在下一次操作或服务轮询时释放，直接导入使用完必须 `close()`。不要与眼动连接并行使用它。

## 连接所有权、退出与错误

1. 上位项目指定一个控制线程或任务持有 TCP 连接，业务模块经它顺序发命令；协议没有请求 ID，不能并发读取或混发后自行猜测归属。
2. 每个新连接先认证，再检查 `STATUS`、`EYES`、`INFO2`；`display=1` 只证明固件启用显示，不能证明两块 TFT 的电气状态。设备身份可用返回的 MAC 核对。
3. ESP32 同时只处理一个 TCP 控制连接。安装校准或手动测试前，上位程序释放连接；完成后重新认证、读回参数再接管。HTTP 服务只负责校准，不提供业务眼动接口。
4. 未认证 3 秒关闭；已认证空闲 12 秒关闭。长期保持但不移动时，可每 5 秒发送只读 `INFO2` 作为保活。设备侧断网后继续显示并追踪最近目标，不自动回中。
5. 客户端 TCP 建连超时 6 秒、发送超时 1 秒、认证/一般回复 5 秒。`ValueError` 是本地参数/令牌校验失败，`RuntimeError` 是设备错误或回复不符，`OSError` / `TimeoutError` 是通信失败或结果未知。
6. 建连/认证阶段尚未发眼动；发送或回复超时后，命令可能已经执行。立即丢弃该连接，重连只读 `POS`、校准和亮度，核对状态后由上位业务决定新目标；不要自动重放结果未知的命令或旧队列。`EyeController` 本身不会在普通请求失败后替调用方关闭连接。
7. 退出是保持、回中还是关闭背光，由上位项目明确决定。关闭连接不等于回中；断网时不能保证任何退出指令完成。现有手动/演示 CLI 正常结束会尝试回中，该策略不隐式作用于导入接口。

UI 连续拖动采用“一次只发一个请求、每屏只保留最新待发值”，最多每 33 ms 发起一次；这是请求调度上限，不是实际帧率。上位项目连续发送目标也应合并过期值，等待本次回复后再发最新值。两屏顺序绘制，同步目标不代表两块面板同一时刻扫描。

## 其他语言：TCP 线协议

使用 ASCII 行协议，每条命令以 `\n` 结束，可接收 `\r\n` 回复。TCP 是字节流，必须缓冲分片直到换行，不能假定一次 `recv()` 就得到一行。固件的 TCP 行缓冲容量为 80 字节：LF 前最多 79 字节，使用 CRLF 时 CR 也占一个字节；上位接入使用 LF。客户端回复缓冲按 128 字节上限处理。

```text
→ AUTH <64 位小写十六进制令牌>
← OK AUTH
→ STATUS
← STATUS alia-eye-wifi-10 <MAC> 1 <屏1目标Y> <屏1已绘制Y>
→ EYES
← EYES 2
→ INFO2
← INFO2 240 320 -285 285 -206 206
→ EYE 1 XY 0 -12
← OKXY 1 0 -12
→ EYE 2 XY 12 0
← OKXY 2 12 0
→ SYNC
← OKSYNC
→ EYE 1 POS
← POS 1 <targetX> <targetY> <drawnX> <drawnY>
```

这是一组协议示例，尖括号为占位符。每条命令都要对应回复；`EYE n` 指定单屏，裸 `XY x y` 同时设置两屏，裸 `POS` 只查询屏幕 1。`OKXY` 是接受目标，`OKSYNC` 是本轮绘制/发送完成，`POS` 是软件目标/已绘制位置，均不等于面板垂直同步或光学读回。

亮度设置/查询为 `EYE 1 BL 8` → `OKBL 1 8`、`EYE 1 BL` → `BL 1 8 82`；校准使用 `EYE 1 CAL <cx> <cy> <angle10> <flipped> 0`；`SAVECAL` → `OKSAVE` 保存两屏。完整请求、回复、诊断字段和非法输入语义见[协议表](OPERATIONS.md#渲染坐标与控制协议)。USB 命令无需 `AUTH`，USB 控制与网络控制仍需由部署方避免同时写入。

协议有令牌认证，无 TLS，仅用于受信任局域网。令牌须为当前用户拥有、权限 `0600` 的普通文件，不写入源码、命令行内容或日志；私密传递到接收方。更换 Wi-Fi/令牌时使用现有配网工具并核对目标 MAC，不能由业务运行时重复配网。

## 当前装配与迁移

最后一次冷启动验证结束后的**已保存**装配值如下，只作为现有设备交接记录，不自动写入其他设备：

| 屏幕 | 中心 | 角度 | 上方定义 | 背光 |
|---|---|---|---|---|
| 1 | `(105,179)` | 0° | 排针端 `connector` | 8% |
| 2 | `(99,180)` | 0° | 排针对侧 `opposite` | 8% |

程序、配网工具、校准 WebUI 和全部图像已随源码提供；运行期不下载图片、不传整帧。当前纹理、原生白底、颜色初始化和 SPI 时序是一组已验收配置，不要为安装方向随意修改反色或重新旋转纹理，使用每屏校准补偿。

现有设备保留 NVS 即可加载这些参数。新设备需自行配网，并在 WebUI 按最终安装位置校准、显式保存。交付包不包含真实令牌、Wi-Fi 密码、设备 NVS 或整片 Flash 备份。MAC、IP、令牌路径、左右眼映射及解释器路径由上位项目环境配置管理。

v10 预编译应用 SHA256 为 `f0ceebb75904ff7f870f996fe81f887709c58d43cddc30476bad9d9b052f0b68`，853,904 字节，源码基线 `0e2a38050743757d169b262ce408f0d3518e2739`。仅适用于经典 ESP32 / 4 MB，应用偏移 `0x10000`，要求已有兼容默认分区表和引导程序；全新板按[源码构建流程](OPERATIONS.md#构建和烧录)上传，不将 app.bin 当完整 Flash 镜像。常规应用上传不主动清除 NVS，擦除 Flash 会失去配置；维护时先记录现有参数。

## 上位项目接收检查

- 校验包外 `SHA256SUMS.txt`，解压后从 `HANDOFF.json` 核对文件、固件版本和未变更的应用摘要；保留 `firmware/NOTICE`。
- 使用接收环境的 IP、私有令牌和目标身份，只读查询版本、双屏能力、范围、校准与背光。
- 在最终装配中分别测试屏幕 1/2 的上、下、左、右，检查未选眼保持目标；测试后由上位程序按约定回中。
- 通过 WebUI 验证中心、角度、上下和独立亮度，显式保存后核对读回；复现一次全断电冷启动，确认新部署也加载保存值。
- 暂停上位控制后进入维护，再退出维护重连读回；验证断网、重连及退出行为符合上位产品需求。
- 保留长期运动与最终线束的颜色/丢图验收。已通过的一次冷启动和用户目视验收，不能替代长稳、信号质量或面板垂直同步测试。
