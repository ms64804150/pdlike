# PerfPilot 构建与分发

一个发布包只能在目标操作系统上原生构建：Windows 产出 `.exe`，macOS 产出 `.app` / `.dmg`。不要在同一个工作目录交叉复用两类 `dist` 产物。

## Windows：便携 ZIP

在 64 位 Windows PowerShell、项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build.ps1
```

产物：

- `dist\PerfPilot\PerfPilot.exe`：运行目录，必须整体保留。
- `dist\PerfPilot-portable-x64.zip`：发给 Windows 用户的压缩包。

脚本会下载 Windows 版 Android platform-tools、运行 `--doctor`，并执行 Android / iOS 采集入口冒烟测试。用户完整解压 ZIP 后双击 `PerfPilot.exe`。

## macOS：DMG

### 构建机要求

- 一台 macOS 机器；Apple Silicon 和 Intel 都可，但产物只适用于构建 Python 所在的架构。
- 原生架构的 Python 3.10+，推荐 Python 3.12。
- `curl`、`ditto`、`hdiutil`（均为 macOS 自带）。
- 首次构建需要网络，以下载 Python 依赖和 macOS 版 Android platform-tools。

将代码放到 Mac 后，在项目根目录运行：

```bash
bash packaging/build-macos.sh
```

若 `python3` 不是目标 Python，可指定：

```bash
PYTHON_BIN=/opt/homebrew/bin/python3.12 bash packaging/build-macos.sh
```

产物：

- `dist/PerfPilot.app`：可直接双击运行的应用。
- `dist/PerfPilot-macos-arm64.dmg` 或 `dist/PerfPilot-macos-x86_64.dmg`：发给 Mac 用户的安装镜像。

用户打开 DMG 后，将 `PerfPilot.app` 拖到“应用程序（Applications）”目录，再从应用程序启动。Android ADB 已内置；iPhone 连接依赖 macOS 自带的 Apple 设备服务，首次连接仍需在手机上解锁并信任电脑。

### 签名与 Gatekeeper

没有 Apple Developer ID 的 DMG 可用于内部测试，但首次打开可能被 Gatekeeper 拦截。用户可在“系统设置 → 隐私与安全性”中确认打开。

正式对外分发应签名并公证。构建时传入 Developer ID：

```bash
MACOS_CODESIGN_IDENTITY='Developer ID Application: Your Company (TEAMID)' \
  bash packaging/build-macos.sh
```

随后用 Apple 的 `notarytool` 提交生成的 DMG，公证成功后执行 `xcrun stapler staple dist/PerfPilot-macos-<arch>.dmg`。签名和公证需要 Apple Developer Program 账号与证书；脚本不会存储任何账号或密钥。

## 自检

Windows：

```powershell
.\dist\PerfPilot\PerfPilot.exe --doctor
```

macOS：

```bash
./dist/PerfPilot.app/Contents/MacOS/PerfPilot --doctor
```

健康输出中的 `portableReady: true` 表示应用自身布局、内置 ADB 和运行依赖可用。设备信任状态不属于包完整性。

## GitHub Releases 更新源

客户端默认查询公开仓库 `ms64804150/pdlike` 的 Latest Release。发布新版本时：

1. 将 `perfpilot/__init__.py` 的 `__version__` 更新为新版本号，例如 `0.3.0`。
2. 分别在 Windows 和 macOS 原生构建。
3. 在 GitHub 创建正式 Release，Tag 使用 `v0.3.0`。
4. 上传**准确使用以下文件名**的附件：
   - `PerfPilot-portable-x64.zip`
   - `PerfPilot-macos-arm64.dmg`（Apple Silicon）
   - `PerfPilot-macos-x86_64.dmg`（Intel，如有）

客户端在“系统设置 → 软件更新”中检查更新；仅当 Release 的版本号更高且包含当前平台对应附件时才提示下载。GitHub Release 附件的 SHA-256 会随 API 返回，供后续自动安装流程校验使用。
