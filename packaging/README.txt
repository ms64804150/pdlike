PerfPilot Windows 客户端（exe）

可以做成「双击即用」，同事不必再装 Python、ADB。管理后台上传本期不做。

能打进包里
- Python 运行时（PyInstaller）
- 本机 Agent + web 页面
- Android platform-tools（adb.exe 及 DLL）
- pymobiledevice3（iOS 采集库）

打不进包、对方电脑仍可能要有
- Android：手机开 USB 调试，首次可能弹「允许调试」
- iPhone：安装 Apple 驱动（Apple Devices 或 iTunes）。这是系统 USB 驱动，无法合法塞进我们的 exe

分发形态（已自动生成并验收便携 ZIP）
1. 运行 packaging\build.ps1
2. 得到 dist\PerfPilot\PerfPilot.exe（同目录还有 _internal）
3. 直接发送 dist\PerfPilot-portable-x64.zip，不要单独发送 exe，也不要手工重打包
4. 对方完整解压 ZIP 后双击 PerfPilot\PerfPilot.exe → 自动打开 http://127.0.0.1:8765
5. 数据仍在本机 %LOCALAPPDATA%\PerfPilot\runs

构建脚本会自动验证
- 内置 adb.exe 可以执行
- pymobiledevice3、Crypto 等关键模块可以导入
- Android / iOS 采集子进程可以启动
- WinTun 和 64 位 Python 运行时已进入包内

对方电脑现场自检
- 在 PerfPilot 目录打开 PowerShell，执行 .\PerfPilot.exe --doctor
- portableReady=true 表示便携包本身完整
- appleUsbmux=false 表示目标电脑缺 Apple USB 服务、手机未解锁/信任，或数据线连接异常
- adbUsable=false 表示内置 ADB 无法执行，通常是安全软件拦截或文件未完整解压
- iosTcpTunnel.ok=false 只影响部分 iOS 17+ TCP 隧道场景，不影响普通 USB 设备和应用枚举

以后要「一个安装 exe」
用 Inno Setup 把 dist\PerfPilot 做成 PerfPilot-Setup-x64.exe，写入开始菜单。逻辑与现在相同。

以后要「可选上传后台」
在本地测完后再 HTTPS 上传 run.json / report.html。本期不接。
