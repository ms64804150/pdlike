# Android / iOS 性能采集器

当前项目是纯 PC 端性能采集工具：Android 使用 ADB，已验证的 iOS 27.0 使用 `pymobiledevice3`。不构建或运行 Android 采集 APK，也不使用设备端 TCP `receiver.py`。

支持范围：

- Android：通过 ADB 采集，支持普通 View、游戏、视频和 SurfaceView。
- iOS 27.0：通过 `pymobiledevice3==11.3.1` 采集，当前推荐使用 64 位 Python 3.12。
- iOS 17.0-17.3：需要额外的管理员隧道流程，暂不作为稳定操作步骤；后续单独处理。

## 采集架构

```text
PC
└── adb_fps.py
    ├── ADB shell 通信
    ├── 前台应用识别
    ├── gfxinfo framestats -> 普通 Android UI FPS
    ├── SurfaceFlinger      -> 游戏 / 视频 / SurfaceView FPS
    ├── /proc/<pid>/stat    -> 目标 App CPU
    ├── dumpsys meminfo     -> 目标 App PSS
    └── 实时值 / 平均值 / 峰值 / Jank
             |
             | USB / Wi-Fi ADB
             v
        Android 设备
```

## 当前采集指标

- 实时 FPS 和会话平均 FPS
- FPS>=18 达标率和 FPS>=25 达标率
- 帧数和 Jank
- 目标 App CPU 当前值、Avg(AppCPU)、峰值和 AppCPU<=60% 达标率
- 目标 App PSS 当前值、Avg(Memory) 和 Peak(Memory)
- 自动识别前台包名
- SurfaceView 游戏和视频帧率
- 可选的实时 FPS、App CPU、App PSS 趋势图
- 停止后生成包含曲线、汇总和采样明细的 HTML 报告

平均 FPS 使用累计新增帧数除以累计有效采样时间：

```text
平均 FPS = 累计新增帧数 / 累计采样时间
```

停止采样时还会输出以下会话指标：

```text
FPS>=18 [%]       FPS 采样窗口达到 18 的百分比
FPS>=25 [%]       FPS 采样窗口达到 25 的百分比
Avg(AppCPU) [%]   有效 App CPU 样本的算术平均值
AppCPU<=60% [%]   有效 App CPU 样本不超过 60% 的百分比
Avg(Memory) [MB]  有效 App PSS 样本的算术平均值
Peak(Memory) [MB] 有效 App PSS 样本的最大值
```

CPU 和内存读取失败的样本不会参与对应平均值和达标率统计。

## 环境要求

- Windows、macOS 或 Linux
- Python 3.10+
- Android SDK Platform Tools，确保 `adb` 在 PATH 中
- 手机开启 USB 调试或无线调试

检查设备连接：

```powershell
adb devices
```

## 运行

### 本地 Web 控制台

Web 控制台由本机 Agent 提供服务，浏览器操作会复用现有 Android / iOS 采集器，不需要额外安装前端依赖：

```powershell
python .\web_server.py
```

然后打开 `http://127.0.0.1:8765`。控制台支持设备发现、iOS 应用列表、能力预检、Session 启停、实时 FPS / CPU / Memory / GPU 状态和 HTML 报告入口。指标不可用时会显示“降级”或“不支持”，不会伪造为 0。关闭服务使用 `Ctrl+C`。

### 统一入口（推荐）

使用 `perf_monitor.py` 可以根据已连接设备自动选择性能采集器：

- 仅连接 Android：调用 `adb_fps.py`。
- 仅连接 iOS 27.0 或更高版本：调用 `ios_perf.py` 和 `pymobiledevice3`。
- iOS 低于 27.0：当前统一入口会停止并提示暂不支持，低版本适配位置已预留。
- 同时连接 Android 和 iOS：必须通过 `--platform` 明确选择，避免采错设备。

Android 自动选择：

```powershell
python perf_monitor.py `
    --platform auto `
    --package com.yalla.hepsioyna `
    --visualize `
    --report android_perf_report.html
```

Android 采集按 `Ctrl+C` 停止；如果命令中带有 `--duration`，统一入口会自动忽略该参数并继续采集。`--duration` 仅对 iOS 采集生效。

iOS 27.0 自动选择：

```powershell
python perf_monitor.py `
    --platform auto `
    --bundle com.yalla.hepsioyna1 `
    --duration 60 `
    --visualize `
    --report ios_perf_report.html
```

如果只连接一种设备，也可以显式指定平台：

```powershell
python perf_monitor.py --platform android --package com.yalla.hepsioyna
python perf_monitor.py --platform ios --bundle com.yalla.hepsioyna1
```

多台设备时分别使用 `--serial`（Android）或 `--udid`（iOS）。统一入口只负责设备发现和参数转发，具体采样指标、实时图表和 HTML 报告仍由对应平台脚本生成。

自动监控当前前台应用：

```powershell
python adb_fps.py
```

指定目标包名：

```powershell
python adb_fps.py --package com.yalla.hepsioyna
```

指定设备和采样等待间隔：

```powershell
python adb_fps.py --serial 39161FDJ9549XN --package com.yalla.hepsioyna --interval 1
```

采样通道可手动指定：

```powershell
python adb_fps.py --mode auto       # 默认，优先 SurfaceFlinger，失败后使用 gfxinfo
python adb_fps.py --mode surface    # 游戏、视频、SurfaceView
python adb_fps.py --mode gfxinfo    # 普通 View
```

按 `Ctrl+C` 停止，会输出会话平均 FPS、累计帧数、App CPU 平均值和峰值、App PSS 峰值。

打开实时可视化窗口：

```powershell
python adb_fps.py --package com.yalla.hepsioyna --visualize
```

停止采样或关闭可视化窗口后，默认在当前目录生成 `adb_fps_report.html`。报告包含 FPS、App CPU、App PSS 趋势图、会话汇总和每个采样窗口的明细。可以通过 `--report` 指定输出路径：

```powershell
python adb_fps.py --report reports\phone-check.html
```

实时图表依赖 Python 自带的 Tkinter；不使用 `--visualize` 时不需要图形界面。

## iOS 27.0 性能采集

### 环境要求

- Windows、macOS 或 Linux；Windows 当前已完成真机验证
- 64 位 Python 3.12
- 已配对并信任电脑的 iPhone
- iPhone 已开启开发者模式，并保持设备解锁
- 目标 App 已安装并正在运行
- 设备可以自动挂载对应的 Developer Disk Image

不要使用 32 位 Python。当前 iOS 采集环境使用独立虚拟环境，避免和 Android 或旧版 `tidevice` 依赖互相影响。

### 安装 iOS 采集环境（Windows）

在项目目录打开 PowerShell：

```powershell
cd D:\pdlike
py -3.12-64 -m venv .venv-ios
.\.venv-ios\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install pymobiledevice3==11.3.1
```

如果系统没有 `py -3.12-64`，请先安装 64 位 Python 3.12，再使用对应的 Python 路径创建虚拟环境。

确认工具可用：

```powershell
.\.venv-ios\Scripts\pymobiledevice3.exe --help
.\.venv-ios\Scripts\pymobiledevice3.exe usbmux list
```

`usbmux list` 应显示设备的 `ProductVersion`、`UniqueDeviceID` 和 `ConnectionType`。

### 查询 Bundle ID

先确认目标应用的 Bundle ID：

```powershell
.\.venv-ios\Scripts\pymobiledevice3.exe apps query com.yalla.hepsioyna1
```

本文档中的 `com.yalla.hepsioyna1` 只是当前测试应用示例，请替换为实际 Bundle ID。Bundle ID 必须与设备上安装的应用完全一致。

### 运行采集

推荐使用 `auto` 后端。iOS 27.0 会使用 `pymobiledevice3`，脚本会自动检查并挂载 Developer Disk Image，然后通过 Bundle ID 定位运行中的 App：

```powershell
cd D:\pdlike
python ios_perf.py `
    --bundle com.yalla.hepsioyna1 `
    --backend auto `
    --interval 1 `
    --duration 60 `
    --visualize `
    --report ios_perf_report.html
```

参数说明：

- `--bundle`：必填，目标 App Bundle ID。
- `--backend auto`：默认优先使用 `pymobiledevice3`。
- `--interval 1`：每秒生成一个报告采样窗口。
- `--duration 60`：采集 60 秒；不传时按 `Ctrl+C` 停止。
- `--visualize`：打开 Tkinter 实时趋势图；不需要图表时可以省略。
- `--report`：指定 HTML 报告路径。
- `--udid`：多台设备连接时指定设备 UDID。

也可以直接使用现代后端：

```powershell
python ios_perf.py `
    --bundle com.yalla.hepsioyna1 `
    --backend pymobiledevice3 `
    --duration 60 `
    --report ios_perf_report.html
```

### iOS 采集指标

- FPS：`CoreAnimationFramesPerSecond`
- App CPU：目标进程的 `cpuUsage`
- Memory：目标进程的 `physFootprint`，报告中显示为 iOS Memory Footprint（MiB）
- GPU：`Device Utilization %`

会话结束时和 HTML 报告中都会包含：

```text
FPS>=18 [%]       FPS 采样窗口达到 18 的百分比
FPS>=25 [%]       FPS 采样窗口达到 25 的百分比
Avg(AppCPU) [%]   有效 App CPU 样本的算术平均值
AppCPU<=60% [%]   有效 App CPU 样本不超过 60% 的百分比
Avg(Memory) [MiB] 有效 Memory Footprint 样本的算术平均值
Peak(Memory) [MiB] 有效 Memory Footprint 样本的最大值
```

各项指标分别使用自己的有效样本计算，缺失的 CPU 或内存数据不会按 0 参与统计。iOS Memory Footprint 与 Android PSS 不是同一个系统指标，报告会明确标注这一点。

### iOS 27.0 常见问题

如果看到 `DeveloperImage not found`，请确认使用的是 64 位 Python 和 `pymobiledevice3==11.3.1`，并重新连接、解锁设备后重试。脚本会自动执行 Developer Disk Image 检查和挂载。

如果 Bundle ID 错误，脚本会在采集开始前提示设备上找不到该 Bundle ID。请先使用 `apps query` 验证完整 ID。

当前已验证的 iOS 27.0 流程不要求手动启动 `remote tunneld`。iOS 17.0-17.3 的旧系统隧道和权限要求暂不写入本节，避免与 iOS 27.0 操作步骤混淆。

## 采集原理

### FPS

普通 View 读取：

```text
adb shell dumpsys gfxinfo <package> framestats
```

游戏、视频和 SurfaceView 读取：

```text
adb shell dumpsys SurfaceFlinger --list
adb shell dumpsys SurfaceFlinger --latency <layer>
```

程序会根据目标包名动态寻找有效图层，并处理图层名称中的特殊字符。

### App CPU

读取：

```text
/proc/<pid>/stat
/proc/stat
```

通过两个采样点的 CPU 时间增量计算目标进程占整机 CPU 能力的比例。进程重启后会自动重置 CPU 基线。

### App PSS

读取：

```text
adb shell dumpsys meminfo <package>
```

使用 `TOTAL PSS` 作为目标 App 内存指标，单位为 MB。

## 权限说明

这些跨应用数据由 ADB shell 读取，不能依赖普通 Android APK：

- `dumpsys gfxinfo`
- `dumpsys SurfaceFlinger`
- `dumpsys meminfo`
- `/proc/<目标 pid>/stat`

因此请保持手机通过 ADB 连接，并在 PC 端运行脚本。普通 APK、无障碍权限或在 Manifest 中声明 `DUMP`，都不能稳定获得这些 shell 级权限。

## 项目结构

```text
perfdog-inject/
├── perf_monitor.py  # 自动识别设备并选择性能采集器
├── adb_fps.py       # Android ADB 性能采集入口
├── ios_perf.py      # iOS 性能采集入口
└── README.md        # 使用、指标和架构说明
```

Android APK、Gradle 工程、TCP 接收器和旧设计资料已从当前实现中移除。`test.py` 和 `device_choose.py` 属于其他自动化/WDA 连接流程，不是本文档中两个性能采集器的启动入口。
