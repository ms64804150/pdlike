# 项目中涉及的 ADB 语句说明

本文档整理本仓库**实际代码**里用到的全部 ADB / `adb shell` 语句，并说明用途与出现位置。

通用约定：多设备时常用 `-s <serial>` 指定设备，例如 `adb -s 39161FDJ9549XN shell ...`。

---

## 1. 设备发现与连接

### `adb devices`

```bash
adb devices
```

**解释：** 列出当前已连接的 Android 设备及状态（`device` / `offline` / `unauthorized` 等）。  
**用途：** 判断是否有可用手机、取第一台设备的序列号。  
**出现位置：** `perf_monitor.py`、`device_choose.py`

---

### `adb devices -l`

```bash
adb devices -l
```

**解释：** 在 `adb devices` 基础上输出更详细信息（如 `model:`、`device:`、`product:`、传输方式等）。  
**用途：** Web 端枚举 Android 设备，用 `model:` 作为显示名，并根据 serial 是否含 `:` 区分 USB / Wi-Fi。  
**出现位置：** `web_server.py`

---

### `adb -s <serial> forward --remove tcp:<port>`

```bash
adb -s <serial> forward --remove tcp:<port>
```

**解释：** 删除指定设备上某本地 TCP 端口的端口转发规则。  
**用途：** 断开/清理设备时移除之前建立的 forward，避免端口残留。  
**出现位置：** `device_choose.py`（通过 Airtest 内置 adb 路径调用）

---

## 2. 应用与进程

### `adb shell pm list packages`

```bash
adb -s <device_id> shell pm list packages
```

**解释：** 列出设备上已安装包名，每行形如 `package:com.example.app`。  
**用途：** Web 会话里拉取 Android 应用列表供用户选择。  
**出现位置：** `web_server.py`

---

### `adb shell dumpsys window`

```bash
adb shell dumpsys window
# 多设备：
adb -s <serial> shell dumpsys window
```

**解释：** 导出 WindowManager 状态，其中含当前焦点窗口信息。  
**用途：** 解析 `mCurrentFocus` / `mFocusedApp`，得到前台应用包名（未指定 `--package` 时自动跟焦）。  
**出现位置：** `adb_fps.py` → `foreground_package()`

---

### `adb shell pidof <package>`

```bash
adb shell pidof com.example.app
```

**解释：** 按包名查找进程 PID；进程不存在时通常无输出。  
**用途：** 定位目标 App 进程，后续读 CPU、内存等。  
**出现位置：** `adb_fps.py` → `_find_pid()`

---

## 3. CPU 采样

### `adb shell cat /proc/<pid>/stat`

```bash
adb shell cat /proc/12345/stat
```

**解释：** 读取指定进程的内核统计信息；其中 `utime`、`stime` 为用户态/内核态占用的 CPU 时钟滴答。  
**用途：** 两次采样差值作为进程 CPU 消耗，再与整机 CPU 滴答对比得到 App CPU%。  
**出现位置：** `adb_fps.py` → `_read_process_ticks()`

---

### `adb shell cat /proc/stat`

```bash
adb shell cat /proc/stat
```

**解释：** 读取整机 CPU 统计；首行 `cpu ...` 各字段之和为系统总时钟滴答。  
**用途：** 作为分母，与进程滴答一起计算 App CPU 占用率。  
**出现位置：** `adb_fps.py` → `_read_system_ticks()`

---

## 4. 内存采样

### `adb shell dumpsys meminfo <package>`

```bash
adb shell dumpsys meminfo com.example.app
```

**解释：** 输出该应用的内存明细；`TOTAL` 行中的 PSS（单位 KB）表示按比例分摊后的物理内存。  
**用途：** 取 TOTAL PSS，换算为 MB，作为 App 内存指标。  
**出现位置：** `adb_fps.py` → `_read_pss()`

---

## 5. FPS / 帧耗时采样

### `adb shell dumpsys gfxinfo <package> framestats`

```bash
adb shell dumpsys gfxinfo com.example.app framestats
```

**解释：** 导出该应用近期帧渲染时间线（含 `---PROFILEDATA---` 等段落）。  
**用途：** 解析帧时间戳，计算实时 FPS、卡顿等；适合普通 View 界面。  
**出现位置：** `adb_fps.py` → `AdbFrameSource.sample()`（`gfxinfo` / `auto` 回退路径）

---

### `adb shell dumpsys SurfaceFlinger --list`

```bash
adb shell dumpsys SurfaceFlinger --list
```

**解释：** 列出 SurfaceFlinger 当前图层（layer）名称。  
**用途：** 按包名匹配相关 layer，再对具体 layer 查 latency。  
**出现位置：** `adb_fps.py` → `_sample_surface()`

---

### `adb shell dumpsys SurfaceFlinger --latency <layer>`

```bash
adb shell dumpsys SurfaceFlinger --latency SurfaceView[com.example.app/...]/]
```

**解释：** 输出指定图层的帧提交时间戳与刷新周期等信息。  
**用途：** 计算游戏/视频/SurfaceView 等场景的 FPS；`auto`/`surface` 模式优先使用。  
**出现位置：** `adb_fps.py` → `_sample_surface()`

---

## 6. 设备信息（Airtest 封装的 shell）

以下通过 `device_choose.py` 中 Airtest 的 `adb.shell(...)` 执行，等价于 `adb shell <命令>`。

### `getprop ro.product.brand`

```bash
adb shell getprop ro.product.brand
```

**解释：** 读取系统属性「厂商品牌」（如 samsung、xiaomi）。  
**用途：** 打印/记录设备品牌。

---

### `getprop ro.product.model`

```bash
adb shell getprop ro.product.model
```

**解释：** 读取设备型号。  
**用途：** 打印/记录机型。

---

### `getprop ro.build.version.release`

```bash
adb shell getprop ro.build.version.release
```

**解释：** 读取 Android 版本号（如 `14`）。  
**用途：** 打印/记录系统版本。

---

### `getprop ro.build.version.sdk`

```bash
adb shell getprop ro.build.version.sdk
```

**解释：** 读取 API Level（如 `34`）。  
**用途：** 打印/记录 SDK 版本。

---

### `wm size`

```bash
adb shell wm size
```

**解释：** 查询当前屏幕物理分辨率（如 `Physical size: 1080x2400`）。  
**用途：** 记录分辨率。

---

### `wm density`

```bash
adb shell wm density
```

**解释：** 查询屏幕密度 DPI（如 `Physical density: 480`）。  
**用途：** 记录密度。

---

## 7. 语句总表

| # | 语句 | 主要用途 | 代码位置 |
|---|------|----------|----------|
| 1 | `adb devices` | 枚举设备 | `perf_monitor.py`、`device_choose.py` |
| 2 | `adb devices -l` | 带详情枚举设备 | `web_server.py` |
| 3 | `adb -s … forward --remove tcp:…` | 清理端口转发 | `device_choose.py` |
| 4 | `adb shell pm list packages` | 列出已装应用 | `web_server.py` |
| 5 | `adb shell dumpsys window` | 识别前台包名 | `adb_fps.py` |
| 6 | `adb shell pidof <package>` | 查进程 PID | `adb_fps.py` |
| 7 | `adb shell cat /proc/<pid>/stat` | 进程 CPU 滴答 | `adb_fps.py` |
| 8 | `adb shell cat /proc/stat` | 整机 CPU 滴答 | `adb_fps.py` |
| 9 | `adb shell dumpsys meminfo <package>` | App PSS 内存 | `adb_fps.py` |
| 10 | `adb shell dumpsys gfxinfo <package> framestats` | View 路径 FPS | `adb_fps.py` |
| 11 | `adb shell dumpsys SurfaceFlinger --list` | 列出图层 | `adb_fps.py` |
| 12 | `adb shell dumpsys SurfaceFlinger --latency <layer>` | Surface 路径 FPS | `adb_fps.py` |
| 13 | `adb shell getprop ro.product.brand` | 品牌 | `device_choose.py` |
| 14 | `adb shell getprop ro.product.model` | 型号 | `device_choose.py` |
| 15 | `adb shell getprop ro.build.version.release` | Android 版本 | `device_choose.py` |
| 16 | `adb shell getprop ro.build.version.sdk` | SDK Level | `device_choose.py` |
| 17 | `adb shell wm size` | 分辨率 | `device_choose.py` |
| 18 | `adb shell wm density` | 屏幕密度 | `device_choose.py` |

---

## 补充说明

- `adb_fps.py` 中所有远程命令都经 `adb_shell()` 包装，实际执行形式为：  
  `adb [-s <serial>] shell '<quoted command>'`。
- `PerfPilot最终架构设计.md` 中还提到规划能力如 `dumpsys batterystats`，**当前代码未调用**，故未列入上表。
- 上述 `dumpsys` / `/proc` 类命令依赖 ADB shell 权限，普通 APK 通常无法稳定获取同等数据。
