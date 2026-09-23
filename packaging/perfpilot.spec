# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.osx import BUNDLE
from PyInstaller.building.build_main import Analysis

try:
    from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, copy_metadata
except ImportError:
    collect_all = None
    collect_dynamic_libs = None
    copy_metadata = None

ROOT = Path(SPECPATH).resolve().parent
datas = [
    (str(ROOT / "web" / "index.html"), "web"),
    (str(ROOT / "web" / "app.js"), "web"),
    (str(ROOT / "web" / "styles.css"), "web"),
]
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
platform_tools = ROOT / "vendor" / ("platform-tools-macos" if IS_MACOS else "platform-tools")
if platform_tools.is_dir():
    # PerfPilot only invokes adb. The rest of platform-tools (fastboot,
    # sqlite3, filesystem utilities, etc.) adds several megabytes but is
    # never used at runtime.
    platform_tool_files = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt") if IS_WINDOWS else ("adb", "NOTICE.txt")
    for name in platform_tool_files:
        source = platform_tools / name
        if source.is_file():
            datas.append((str(source), "vendor/platform-tools"))

binaries = []
hiddenimports = [
    "adb_fps",
    "ios_perf",
    "perfpilot",
    "perfpilot.__main__",
    "perfpilot.runtime",
    "perfpilot.server",
    "perfpilot.session",
    "perfpilot.devices",
    "perfpilot.watch",
    "perfpilot.collectors",
    "perfpilot.android_fg",
    "perfpilot.ios_lockdown",
    "perfpilot.win_compat",
    "perfpilot.diagnose",
    "pymobiledevice3",
    "pymobiledevice3.lockdown",
    "pymobiledevice3.usbmux",
    "pymobiledevice3.services.installation_proxy",
    "pyimg4",
    "ipsw_parser",
    "developer_disk_image",
    "lzss",
    "lzfse",
    "qh3",
    "sslpsk_pmd3",
    "pytun_pmd3",
    "Crypto",
    "Crypto.Cipher",
    "Crypto.Cipher.AES",
]


def _include_package(name: str, recursive_meta: bool = False) -> None:
    if copy_metadata is not None:
        try:
            datas.extend(copy_metadata(name, recursive=recursive_meta))
        except Exception:
            try:
                datas.extend(copy_metadata(name))
            except Exception:
                pass
    if collect_all is not None:
        try:
            # Python modules are already stored in the PYZ. Copying their
            # source files again as data needlessly duplicates them.
            pkg_datas, pkg_binaries, pkg_hidden = collect_all(name, include_py_files=False)
            datas.extend(pkg_datas)
            binaries.extend(pkg_binaries)
            hiddenimports.extend(pkg_hidden)
        except Exception:
            pass


_include_package("pymobiledevice3", recursive_meta=True)
for extra in (
    "pyimg4",
    "ipsw_parser",
    "developer_disk_image",
    "qh3",
    "sslpsk_pmd3",
    "Crypto",
    "pytun_pmd3",
):
    _include_package(extra)

# This is an x64-only build. collect_all("pytun_pmd3") also brings WinTun
# binaries for x86, ARM, and ARM64; retain only the amd64 runtime.
def _keep_x64_wintun(item: tuple[str, str]) -> bool:
    destination = str(item[1]).replace("\\", "/").lower()
    return "pytun_pmd3/wintun/bin/" not in destination or "/amd64" in destination


if IS_WINDOWS:
    datas = [item for item in datas if _keep_x64_wintun(item)]
    binaries = [item for item in binaries if _keep_x64_wintun(item)]
    wintun_dll = ROOT / "vendor" / "wintun" / "amd64" / "wintun.dll"
    if wintun_dll.is_file():
        binaries.append((str(wintun_dll), "pytun_pmd3/wintun/bin/amd64"))
    venv_wintun = Path(sys.prefix) / "Lib" / "site-packages" / "pytun_pmd3" / "wintun" / "bin" / "amd64" / "wintun.dll"
    if venv_wintun.is_file() and venv_wintun.resolve() != wintun_dll.resolve():
        binaries.append((str(venv_wintun), "pytun_pmd3/wintun/bin/amd64"))

site_packages = Path(sys.prefix) / "Lib" / "site-packages"
if IS_WINDOWS:
    for pyd in site_packages.glob("*.pyd"):
        if pyd.name.lower().startswith(("lzss", "lzfse")):
            binaries.append((str(pyd), "."))
if collect_dynamic_libs is not None:
    for name in ("qh3", "sslpsk_pmd3", "Crypto"):
        try:
            binaries.extend(collect_dynamic_libs(name))
        except Exception:
            pass

if IS_WINDOWS:
    dll_dir = Path(sys.base_prefix) / "DLLs"
    for name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll", "libssl-1_1-x64.dll", "libcrypto-1_1-x64.dll"):
        candidate = dll_dir / name
        if candidate.is_file():
            binaries.append((str(candidate), "."))

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        # Optional pymobiledevice3 features not used by PerfPilot. PyAV and
        # Pillow support screen streaming/WebInspector, while IPython and
        # its helpers only support interactive developer shells.
        "av",
        "PIL",
        "IPython",
        "jedi",
        "parso",
        "matplotlib_inline",
        "stack_data",
        "asttokens",
        "executing",
        "pure_eval",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe_options = {"console": True}
if IS_WINDOWS:
    # Preserve stdout/stderr for --doctor and collector pipes, but hide the
    # console immediately when users launch the executable by double-click.
    exe_options["hide_console"] = "hide-early"
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PerfPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
    **exe_options,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PerfPilot",
)

if IS_MACOS:
    app = BUNDLE(
        coll,
        name="PerfPilot.app",
        bundle_identifier="com.perfpilot.agent",
        info_plist={
            "CFBundleDisplayName": "PerfPilot",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
