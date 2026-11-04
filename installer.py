# -*- coding: utf-8 -*-
"""Clash WiFi Kit 安装程序。

行为：
    1. 结束正在运行的实例
    2. 将随包的程序文件复制到 %LOCALAPPDATA%\\Programs\\ClashWiFiKit
    3. 创建桌面与开始菜单快捷方式
    4. 写入「设置 → 应用」中的卸载条目
    5. 询问是否立即启动

卸载：安装目录下的「卸载.cmd」，或 设置 → 应用 → Clash WiFi Kit。
"""

import os
import shutil
import subprocess
import sys
import winreg

APP_NAME = "Clash WiFi Kit"
APP_VERSION = "1.0"
FOLDER = "ClashWiFiKit"
KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ClashWiFiKit"

BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BUNDLE, "app")
TARGET = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                      "Programs", FOLDER)


def line(msg=""):
    print(msg, flush=True)


def stop_running():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "ClashWiFiKit.exe"],
                       capture_output=True, creationflags=0x08000000)
    except Exception:
        pass


def copy_tree():
    if not os.path.isdir(SRC):
        raise RuntimeError("安装包损坏：未找到 app 目录")
    if os.path.isdir(TARGET):
        shutil.rmtree(TARGET, ignore_errors=True)
    shutil.copytree(SRC, TARGET)
    return sum(len(f) for _, _, f in os.walk(TARGET))


def make_shortcuts(exe):
    ps = (
        "$s = New-Object -ComObject WScript.Shell\n"
        "$t = @([Environment]::GetFolderPath('Desktop'),"
        " (Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs'))\n"
        "foreach ($d in $t) {\n"
        "  if (-not (Test-Path $d)) { continue }\n"
        f"  $l = $s.CreateShortcut((Join-Path $d '{APP_NAME}.lnk'))\n"
        f"  $l.TargetPath = '{exe}'\n"
        f"  $l.WorkingDirectory = '{TARGET}'\n"
        f"  $l.IconLocation = '{exe}'\n"
        "  $l.Description = 'Clash 出站绑定与路由检测'\n"
        "  $l.Save()\n"
        "}\n"
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", ps], capture_output=True, creationflags=0x08000000)


def write_registry(exe):
    uninstall_cmd = os.path.join(TARGET, "卸载.cmd")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY_PATH) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, "Ta0X1")
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, TARGET)
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ, exe)
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_cmd}"')
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)


def main():
    line()
    line("  " + "=" * 52)
    line(f"  {APP_NAME} {APP_VERSION}  安装程序")
    line("  " + "=" * 52)
    line()
    line(f"  安装位置：{TARGET}")
    line()
    try:
        line("  [1/4] 结束正在运行的实例 ...")
        stop_running()

        line("  [2/4] 复制程序文件 ...")
        n = copy_tree()
        line(f"        已复制 {n} 个文件")

        exe = os.path.join(TARGET, "ClashWiFiKit.exe")
        if not os.path.isfile(exe):
            raise RuntimeError("复制后未找到 ClashWiFiKit.exe")

        line("  [3/4] 创建快捷方式 ...")
        make_shortcuts(exe)

        line("  [4/4] 写入卸载条目 ...")
        write_registry(exe)

        line()
        line("  安装完成。")
        line("  已创建：桌面快捷方式、开始菜单快捷方式、设置中的应用条目")
        line()
        ans = input("  是否立即启动？[Y/n] ").strip().lower()
        if ans not in ("n", "no"):
            subprocess.Popen([exe], cwd=TARGET, creationflags=0x00000008)
        line()
        line("  可按任意键关闭本窗口。")
        try:
            input()
        except Exception:
            pass
        return 0
    except Exception as e:
        line()
        line(f"  安装失败：{type(e).__name__}: {e}")
        line()
        try:
            input("  按回车退出。")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
