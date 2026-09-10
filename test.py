"""
iOS自动化环境检查和WDA启动工具
"""
import subprocess
import sys
from utils.ios_device_manager import IDeviceManager


def check_environment():
    """检查环境配置"""
    print("=" * 60)
    print("🔍 iOS自动化环境检查")
    print("=" * 60)
    
    # 检查tidevice
    try:
        result = subprocess.run(['tidevice', '--version'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print(f"✅ tidevice已安装: {result.stdout.strip()}")
        else:
            print("❌ tidevice未正确安装")
            return False
    except FileNotFoundError:
        print("❌ 未找到tidevice命令，请执行: pip install tidevice")
        return False
    
    # 检查设备连接
    print("\n📱 检查设备连接...")
    devices = IDeviceManager.list_devices()
    
    if not devices:
        print("❌ 未检测到iOS设备")
        print("\n请确认:")
        print("  1. iPhone已通过USB线连接到电脑")
        print("  2. 在iPhone上点击了'信任此电脑'")
        print("  3. 输入了锁屏密码")
        return False
    
    print(f"✅ 检测到 {len(devices)} 个设备:")
    for i, device in enumerate(devices, 1):
        print(f"   {i}. {device['name']} (UDID: {device['udid']})")
    
    return True


def show_app_list(udid=None):
    """显示设备上安装的应用"""
    print("\n" + "=" * 60)
    print("📋 应用列表")
    print("=" * 60)
    
    apps = IDeviceManager.list_apps(udid)
    
    if not apps:
        print("❌ 未获取到应用列表")
        return
    
    print(f"共找到 {len(apps)} 个应用:\n")
    for i, app in enumerate(apps, 1):
        print(f"{i:3d}. {app['name']}")
        print(f"      Bundle ID: {app['bundle_id']}\n")


def start_wda(bundle_id=None, udid=None):
    """启动WDA服务"""
    print("\n" + "=" * 60)
    print("🚀 启动WebDriverAgent (WDA)")
    print("=" * 60)
    
    if not udid:
        devices = IDeviceManager.list_devices()
        if not devices:
            print("❌ 未检测到设备")
            return False
        udid = devices[0]['udid']
        print(f"使用设备: {devices[0]['name']}")
    
    # WDA的Bundle ID（通常是这个）
    wda_bundle_id = "com.facebook.WebDriverAgentRunner.xctrunner"
    
    print(f"\n正在启动WDA服务...")
    print(f"设备UDID: {udid}")
    print(f"WDA Bundle ID: {wda_bundle_id}")
    print("\n⏳ WDA服务将在后台运行，按 Ctrl+C 停止\n")
    
    try:
        # 启动WDA
        cmd = ['tidevice', '-u', udid, 'xctest', '-B', wda_bundle_id]
        
        if bundle_id:
            print(f"目标应用: {bundle_id}")
            # 先启动目标应用
            IDeviceManager.start_app(bundle_id, udid)
        
        print("\n💡 提示:")
        print("  - WDA服务启动后，可以通过 http://localhost:8100 访问")
        print("  - 保持此窗口开启以维持WDA服务")
        print("  - 按 Ctrl+C 可停止WDA服务\n")
        
        # 执行命令（会阻塞）
        subprocess.run(cmd)
        
    except KeyboardInterrupt:
        print("\n\n⏹️  WDA服务已停止")
    except Exception as e:
        print(f"\n❌ 启动失败: {str(e)}")
        return False
    
    return True


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("📱 iOS自动化测试工具")
    print("=" * 60)
    print("\n请选择操作:")
    print("1. 检查环境和设备")
    print("2. 查看应用列表")
    print("3. 启动WDA服务")
    print("4. 截图")
    print("0. 退出")
    print()
    
    choice = input("请输入选项 (0-4): ").strip()
    
    if choice == "1":
        check_environment()
    
    elif choice == "2":
        if check_environment():
            devices = IDeviceManager.list_devices()
            udid = devices[0]['udid'] if devices else None
            show_app_list(udid)
    
    elif choice == "3":
        if check_environment():
            devices = IDeviceManager.list_devices()
            udid = devices[0]['udid'] if devices else None
            
            # 询问是否指定应用
            use_app = input("\n是否指定要测试的应用? (y/n): ").strip().lower()
            bundle_id = None
            
            if use_app == 'y':
                apps = IDeviceManager.list_apps(udid)
                if apps:
                    show_app_list(udid)
                    idx = input("\n请输入应用序号: ").strip()
                    try:
                        idx = int(idx) - 1
                        if 0 <= idx < len(apps):
                            bundle_id = apps[idx]['bundle_id']
                            print(f"已选择: {apps[idx]['name']}")
                        else:
                            print("无效的序号")
                            return
                    except ValueError:
                        print("请输入有效的数字")
                        return
            
            start_wda(bundle_id, udid)
    
    elif choice == "4":
        if check_environment():
            devices = IDeviceManager.list_devices()
            udid = devices[0]['udid'] if devices else None
            
            output_path = input("\n请输入截图保存路径 (默认: screenshot.png): ").strip()
            if not output_path:
                output_path = "screenshot.png"
            
            try:
                IDeviceManager.screenshot(udid, output_path)
                print(f"✅ 截图已保存: {output_path}")
            except Exception as e:
                print(f"❌ 截图失败: {str(e)}")
    
    elif choice == "0":
        print("再见！")
        sys.exit(0)
    
    else:
        print("无效的选项")


if __name__ == "__main__":
    try:
        while True:
            main()
            input("\n按回车键继续...")
    except KeyboardInterrupt:
        print("\n\n程序已退出")
        sys.exit(0)
