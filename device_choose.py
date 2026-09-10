"""
Driver工厂类 - 用于创建和管理Airtest Driver和Poco
"""
import atexit
import subprocess
from airtest.core.api import connect_device, device
from poco.drivers.android.uiautomation import AndroidUiautomationPoco
from config.config import APP_CONFIG, AIRTEST_CONFIG
from utils.logger import log
from utils.ios_device_manager import IDeviceManager


class DriverFactory:
    """Driver工厂类"""

    _device = None
    _poco = None
    _platform = None
    _wda_process = None  # 保存WDA进程引用
    _wda_start_time = None  # 记录WDA启动时间

    @classmethod
    def get_driver(cls, platform="android"):
        """
        获取driver实例（返回device对象）
        :param platform: 平台类型 android 或 ios
        :return: Airtest device实例
        """
        if cls._device is None or cls._platform != platform:
            cls.quit_driver()
            cls._device = cls._create_driver(platform)
            cls._platform = platform
        return cls._device

    @classmethod
    def get_poco(cls):
        """
        获取Poco实例
        :return: Poco实例
        """
        if cls._poco is None:
            if cls._platform == "ios":
                from poco.drivers.ios import iosPoco
                wda_url = getattr(cls._device, 'wda_url', None)
                cls._poco = iosPoco(device=cls._device) if wda_url else iosPoco()
            else:
                cls._poco = AndroidUiautomationPoco(use_airtest_input=True, screenshot_each_action=False)
        return cls._poco

    @classmethod
    def _create_driver(cls, platform):
        """
        创建driver实例
        :param platform: 平台类型
        :return: Airtest device实例
        """
        try:
            # 禁用Airtest的DEBUG日志，避免输出混乱
            import logging
            airtest_logger = logging.getLogger('airtest')
            airtest_logger.setLevel(logging.WARNING)  # 只显示WARNING及以上级别

            # 根据平台连接设备
            if platform.lower() == "android":
                # 自动检测 Android 设备序列号
                serial = AIRTEST_CONFIG.get("android_serial", APP_CONFIG["android"]["deviceName"])
                try:
                    result = subprocess.run(['adb', 'devices'], capture_output=True, text=True, timeout=5)
                    if result.returncode == 0 and 'device' in result.stdout:
                        lines = result.stdout.strip().split('\n')[1:]
                        connected_devices = [line for line in lines if 'device' in line and not line.startswith('*')]
                        if connected_devices:
                            serial = connected_devices[0].split('\t')[0]
                            log.info(f"🔍 自动检测到 Android 设备: {serial}")
                        else:
                            log.warning(f"⚠️ 未检测到设备，使用配置: {serial}")
                    else:
                        log.warning(f"⚠️ ADB 未就绪，使用配置: {serial}")
                except Exception as e:
                    log.warning(f"⚠️ 检测设备失败，使用配置: {serial} ({str(e)})")
                
                cap_method = AIRTEST_CONFIG.get("cap_method", "JAVACAP")
                touch_method = AIRTEST_CONFIG.get("touch_method", "ADBTOUCH")

                connect_str = f"Android:///{serial}?cap_method={cap_method}&touch_method={touch_method}"
                dev = connect_device(connect_str)
                cls._disable_airtest_cleanup()
                cls._log_android_device_info(dev)
                return dev

            elif platform.lower() == "ios":
                return cls._connect_ios_device()
            else:
                raise ValueError(f"不支持的平台: {platform}")

        except Exception as e:
            log.error(f"创建driver失败: {str(e)}")
            raise

    @classmethod
    def _connect_ios_device(cls):
        """
        连接iOS设备
        :return: iOS device实例
        """
        try:
            log.info("🔍 检测设备...")
            devices = IDeviceManager.list_devices()
            
            if not devices:
                raise Exception("未检测到iOS设备")

            configured_udid = APP_CONFIG["ios"].get("udid", "")
            if configured_udid and configured_udid != "":
                matched_device = next((d for d in devices if d['udid'] == configured_udid), None)
                if matched_device:
                    udid = matched_device['udid']
                    device_name = matched_device['name']
                else:
                    log.warning(f"⚠️ 配置的设备未找到，使用第一个可用设备")
                    udid = devices[0]['udid']
                    device_name = devices[0]['name']
            else:
                udid = devices[0]['udid']
                device_name = devices[0]['name']

            bundle_id = AIRTEST_CONFIG.get("ios_bundle_id") or APP_CONFIG["ios"].get("bundleId")
            if not bundle_id:
                apps = IDeviceManager.list_apps(udid)
                if apps:
                    log.info(f"📱 已安装应用（前10个）:")
                    for app in apps[:10]:
                        log.info(f"  - {app['name']} ({app['bundle_id']})")
                raise Exception("请在config.py中配置ios_bundle_id参数")

            APP_CONFIG["ios"].update({"udid": udid, "deviceName": device_name, "bundleId": bundle_id})

            # 查找WDA Bundle ID
            configured_wda = APP_CONFIG["ios"].get("wda_bundle_id", "")
            if configured_wda:
                wda_bundle_id = configured_wda
            else:
                log.info("⚠️ 未配置WDA Bundle ID，尝试自动检测...")
                apps = IDeviceManager.list_apps(udid)
                wda_bundle_id = None
                
                for app in apps:
                    if 'WebDriverAgent' in app['bundle_id'] or 'WDA' in app['bundle_id'].upper():
                        wda_bundle_id = app['bundle_id']
                        break
                
                if not wda_bundle_id:
                    for possible_id in ['com.facebook.WebDriverAgentRunner.xctrunner', 'com.ygdemo.WebDriverAgentRunner.xctrunner']:
                        if any(app['bundle_id'] == possible_id for app in apps):
                            wda_bundle_id = possible_id
                            break
                
                if not wda_bundle_id:
                    raise Exception("未找到WebDriverAgent，请使用Xcode安装WDA到设备")

            # 启动WDA服务
            wda_port = AIRTEST_CONFIG.get("ios_wda_port", 8100)
            if cls._check_wda_status(wda_port):
                log.info(f"✅ WDA已在端口 {wda_port} 运行")
            else:
                cls._start_wda_service(udid, wda_bundle_id, wda_port)
                if not cls._wait_for_wda_ready(wda_port, timeout=15):
                    log.warning("⚠️ WDA可能未完全就绪")

            # 连接iOS设备
            ios_connect_str = f"iOS:///http://localhost:{wda_port}/?udid={udid}"
            try:
                dev = connect_device(ios_connect_str)
                return dev
            except Exception as e:
                log.warning(f"⚠️ Airtest原生连接失败: {str(e)}")
                
                # 如果原生连接失败，使用自定义包装器
                class IOS:
                    def __init__(self, udid, bundle_id, port):
                        self.udid = udid
                        self.bundle_id = bundle_id
                        self.platform = "iOS"
                        self.wda_port = port
                        self.wda_url = f"http://localhost:{port}"
                        self.uuid = udid
                    
                    def snapshot(self, filename=None):
                        """截图"""
                        import tempfile
                        import os
                        if not filename:
                            temp_file = os.path.join(tempfile.gettempdir(), f"ios_snapshot_{self.udid[:8]}.png")
                        else:
                            temp_file = filename
                        IDeviceManager.screenshot(self.udid, temp_file)
                        return temp_file
                    
                    def shell(self, cmd):
                        """执行shell命令"""
                        result = subprocess.run(
                            ['tidevice', '-u', self.udid] + cmd.split(),
                            capture_output=True,
                            text=True,
                            timeout=10
                        )
                        return result.stdout
                    
                    def start_app(self, package, activity=None):
                        """启动应用"""
                        IDeviceManager.start_app(package or self.bundle_id, self.udid)
                    
                    def stop_app(self, package, activity=None):
                        """停止应用"""
                        IDeviceManager.stop_app(package or self.bundle_id, self.udid)
                    
                    def list_app(self, third_only=False):
                        """列出应用"""
                        apps = IDeviceManager.list_apps(self.udid)
                        if third_only:
                            return [app['bundle_id'] for app in apps]
                        return apps
                    
                    def get_current_application(self):
                        """获取当前应用"""
                        return self.bundle_id

                ios_device = IOS(udid, bundle_id, wda_port)
                try:
                    from airtest.core.api import G
                    G.DEVICE = ios_device
                except Exception:
                    pass
                return ios_device

        except Exception as e:
            log.error(f"连接iOS设备失败: {str(e)}")
            raise

    @classmethod
    def _log_android_device_info(cls, dev):
        """打印 Android 设备信息"""
        try:
            adb = getattr(dev, 'adb', None)
            if adb is None:
                log.debug("dev.adb 为 None，跳过设备信息获取")
                return
            brand = adb.shell("getprop ro.product.brand").strip()
            model = adb.shell("getprop ro.product.model").strip()
            android_ver = adb.shell("getprop ro.build.version.release").strip()
            sdk = adb.shell("getprop ro.build.version.sdk").strip()
            resolution = adb.shell("wm size").strip().split(": ")[-1]
            density = adb.shell("wm density").strip().split(": ")[-1]
            serial = getattr(adb, 'serial', 'unknown')
            log.info(f"📱 Android 设备信息: {brand} {model} | {resolution} | Android {android_ver} (SDK {sdk}) | 序列号: {serial}")
        except Exception as e:
            log.error(f"获取设备信息失败: {e}")
            import traceback
            log.debug(traceback.format_exc())

    @classmethod
    def _disable_airtest_cleanup(cls):
        """
        禁用Airtest的atexit清理函数，避免退出时的日志错误
        """
        try:
            # 方法1: 尝试从atexit中移除
            import airtest.utils.snippet as snippet_module

            if hasattr(snippet_module, 'exitfunc'):
                try:
                    result = atexit.unregister(snippet_module.exitfunc)
                    if result:
                        log.debug("✅ 方法1成功: 已从atexit移除Airtest清理函数")
                        return
                except Exception:
                    pass

            # 方法2: 替换_cleanup函数为空函数（关键！）
            try:
                if hasattr(snippet_module, '_cleanup'):
                    def noop_cleanup():
                        pass

                    snippet_module._cleanup = noop_cleanup
                    log.debug("✅ 方法2成功: 已替换_cleanup为空函数")
            except Exception:
                pass

            # 方法3: 替换ADB清理函数
            try:
                import airtest.core.android.adb as adb_module

                if hasattr(adb_module, 'cleanup_adb_forward'):
                    def noop_cleanup():
                        pass

                    adb_module.cleanup_adb_forward = noop_cleanup
                    log.debug("✅ 方法3成功: 已替换cleanup_adb_forward为空函数")
            except Exception:
                pass

        except Exception:
            pass  # 静默失败，不影响测试

    @classmethod
    def quit_driver(cls):
        """关闭driver"""
        if cls._device:
            try:
                from airtest.core.api import stop_app
                app_package = APP_CONFIG["android"]["appPackage"]
                stop_app(app_package)
                log.info(f"应用已关闭: {app_package}")
            except Exception as e:
                log.error(f"关闭应用失败: {str(e)}")

            # 清理ADB转发，避免退出时的日志错误
            try:
                # 手动移除所有forward
                adb = cls._device.adb
                if adb:
                    # 获取当前设备的serial
                    serial = adb.serial
                    # 使用adb命令移除forward（静默执行，不记录日志）
                    import subprocess
                    import os
                    adb_path = os.path.join(os.path.dirname(adb.builtin_adb_path()), 'adb.exe') if hasattr(adb, 'builtin_adb_path') else 'adb'

                    # 尝试移除常见的forward端口
                    for port in [10080, 10081]:  # Poco使用的端口
                        try:
                            subprocess.run(
                                [adb_path, '-s', serial, 'forward', '--remove', f'tcp:{port}'],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=2
                            )
                        except Exception:
                            pass  # 忽略错误

                    log.debug("ADB forward已清理")
            except Exception as e:
                log.debug(f"清理ADB forward时出错（可忽略）: {str(e)}")
            finally:
                cls._device = None
                cls._poco = None
                log.info("Driver已关闭")
    
    @classmethod
    def reset_driver(cls):
        """重置driver"""
        cls.quit_driver()
    
    @classmethod
    def _check_wda_status(cls, port=8100):
        """
        检查WDA是否在指定端口运行
        :param port: WDA端口
        :return: True if WDA is running, False otherwise
        """
        import requests
        try:
            response = requests.get(f"http://localhost:{port}/status", timeout=2)
            return response.status_code == 200
        except Exception:
            return False
    
    @classmethod
    def _wait_for_wda_ready(cls, port=8100, timeout=15):
        """
        等待WDA服务就绪
        :param port: WDA端口
        :param timeout: 超时时间（秒）- 减少到15秒
        :return: True if WDA is ready, False if timeout
        """
        import time
        start_time = time.time()
        while time.time() - start_time < timeout:
            if cls._check_wda_status(port):
                return True
            time.sleep(1)
        log.warning(f"⚠️ WDA就绪超时")
        return False
    
    @classmethod
    def _start_wda_service(cls, udid, wda_bundle_id, wda_port):
        """
        启动WDA服务
        :param udid: 设备UDID
        :param wda_bundle_id: WDA Bundle ID
        :param wda_port: WDA端口
        """
        import threading
        import time
        
        # 如果已有WDA进程且在运行，先检查是否可用
        if cls._wda_process is not None:
            if cls._check_wda_status(wda_port):
                log.info(f"✅ 复用已有的WDA服务（端口: {wda_port}）")
                return
            else:
                log.warning("⚠️ 之前的WDA服务已不可用，将重新启动")
                cls._wda_process = None
        
        # 检查是否需要使用go-ios tunnel (iOS 17+)
        use_go_ios = APP_CONFIG["ios"].get("use_go_ios_tunnel", False)
        
        if use_go_ios:
            log.info("🔧 检测到iOS 17+配置，尝试使用go-ios tunnel...")
            if cls._try_start_with_go_ios(udid, wda_bundle_id, wda_port):
                return
            else:
                log.warning("⚠️ go-ios tunnel启动失败，回退到tidevice方式")
        
        # 使用tidevice xctest启动WDA
        def start_wda():
            try:
                process = subprocess.Popen(
                    ['tidevice', '-u', udid, 'xctest', '-B', wda_bundle_id],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                cls._wda_process = process
                process.wait()
            except Exception as e:
                log.error(f"❌ WDA启动异常: {str(e)}")
                cls._wda_process = None
        
        # 在后台线程启动WDA
        wda_thread = threading.Thread(target=start_wda, daemon=True)
        wda_thread.start()
    
    @classmethod
    def _try_start_with_go_ios(cls, udid, wda_bundle_id, wda_port):
        """
        尝试使用go-ios启动WDA (适用于iOS 17+)
        :param udid: 设备UDID
        :param wda_bundle_id: WDA Bundle ID
        :param wda_port: WDA端口
        :return: True if successful, False otherwise
        """
        import threading
        import time
        
        try:
            # 检查go-ios是否安装
            result = subprocess.run(['ios', '--version'], capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                log.warning("⚠️ go-ios未安装，请先执行: npm install -g go-ios")
                return False
            
            log.info("✅ go-ios已安装")
            
            # 检查tunnel是否已经在运行
            tunnel_result = subprocess.run(['ios', 'tunnel', 'ls'], capture_output=True, text=True, timeout=5)
            if udid in tunnel_result.stdout:
                log.info("✅ go-ios tunnel已在运行")
            else:
                log.info("🚀 启动go-ios tunnel...")
                # 启动tunnel（后台运行）
                tunnel_process = subprocess.Popen(
                    ['ios', 'tunnel', 'start'],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                # 等待tunnel启动
                time.sleep(5)
                log.info("✅ go-ios tunnel已启动")
            
            # 使用go-ios启动WDA
            def start_wda_with_goios():
                try:
                    log.info(f"🔄 使用go-ios启动WDA: ios runwda --bundleid {wda_bundle_id} --udid {udid}")
                    process = subprocess.Popen(
                        ['ios', 'runwda', '--bundleid', wda_bundle_id, '--udid', udid],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    cls._wda_process = process
                    process.wait()
                except Exception as e:
                    log.error(f"❌ go-ios WDA启动异常: {str(e)}")
                    cls._wda_process = None
            
            # 在后台线程启动WDA
            wda_thread = threading.Thread(target=start_wda_with_goios, daemon=True)
            wda_thread.start()
            
            return True
            
        except FileNotFoundError:
            log.warning("⚠️ go-ios命令未找到，请安装: npm install -g go-ios")
            return False
        except Exception as e:
            log.warning(f"⚠️ go-ios启动失败: {str(e)}")
            return False
    
    @classmethod
    def cleanup_wda(cls):
        """
        清理WDA服务（在全部测试完成后调用）
        可以在conftest.py的pytest_sessionfinish中调用
        """
        if cls._wda_process is not None:
            try:
                log.info("🛑 正在停止WDA服务...")
                cls._wda_process.terminate()
                cls._wda_process.wait(timeout=5)
                cls._wda_process = None
                cls._wda_start_time = None
                log.info("✅ WDA服务已停止")
            except Exception as e:
                log.warning(f"⚠️ 停止WDA服务时出错: {str(e)}")
                cls._wda_process = None