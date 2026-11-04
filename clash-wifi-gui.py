#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash WiFi Kit — 图形界面

功能：
    1. 自动定位 Clash Verge 的安装路径、内核与配置目录
    2. 检测网卡、路由、配置是否正确，判断 Clash 是否只从无线网卡出站
    3. 写入 interface-name、设置网卡优先级
    4. 验证出口分布，执行断网测试
    5. 管理看门狗进程
    6. 记录每一次变更，支持撤销与全部还原

运行：
    python clash-wifi-gui.py
    python clash-wifi-gui.py --selftest     仅输出文本诊断，不启动界面

依赖：Windows 10/11，Clash Verge Rev + mihomo；无第三方 Python 库。
"""

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ------------------------------------------------------------------ 常量
APP_NAME = "Clash WiFi Kit"
APP_VERSION = "1.0"
SITE_URL = "https://blog.taoxi.ink"
SITE_NAME = "Ta0X1 の Blog"
KEY = "interface-name"
WIFI_RE = re.compile(r"WLAN|Wi-?Fi|Wireless|无线", re.I)
CREATE_NO_WINDOW = 0x08000000

def _app_dir():
    """工作目录：打包后为 exe 所在目录；该目录不可写时退回 LOCALAPPDATA。"""
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    try:
        probe = os.path.join(base, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
        return base
    except Exception:
        alt = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                           "ClashWiFiKit")
        try:
            os.makedirs(alt, exist_ok=True)
        except Exception:
            pass
        return alt


HERE = _app_dir()
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE)
HISTORY_FILE = os.path.join(HERE, "clash-wifi-history.json")
LOG_FILE = os.path.join(HERE, "clash-wifi-gui.log")

ASSETS = ("clash-watchdog.ps1", "Find-Clash.ps1", "clash-wifi.ps1", "README.md")


def ensure_assets():
    """打包运行时把随包的 PowerShell 脚本释放到工作目录，供外部进程调用。"""
    missing = []
    for name in ASSETS:
        dst = os.path.join(HERE, name)
        if not os.path.isfile(dst):
            src = os.path.join(BUNDLE_DIR, name)
            if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
        if not os.path.isfile(dst):
            missing.append(name)
    return missing

TASK_NAME = "ClashWatchdog"
AUTOSTART_CMD = os.path.join(HERE, "clash-watchdog-autostart.cmd")

TEST_URLS = [
    "http://myip.ipip.net",
    "http://ip-api.com/line/?fields=query",
    "https://api.ipify.org",
]

C_BG = "#f5f7fa"
C_CARD = "#ffffff"
C_OK = "#1a7f37"
C_WARN = "#9a6700"
C_BAD = "#cf222e"
C_SKIP = "#6e7781"
C_INK = "#1f2328"

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)
FONT_H = ("Microsoft YaHei UI", 13, "bold")
FONT_MONO = ("Consolas", 9)

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ================================================================== 基础工具
def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except Exception:
        return ""
    raw = p.stdout or b""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def elevate():
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, elevate_args(), None, 1)
        return True
    except Exception:
        return False


def set_proxy_enable(value):
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                           0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, int(value))
        back = winreg.QueryValueEx(k, "ProxyEnable")[0]
        winreg.CloseKey(k)
        if int(back) != int(value):
            return False, "写入后回读不一致"
        try:
            ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)
            ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)
        except Exception:
            pass
        return True, ""
    except PermissionError:
        return False, "权限不足，无法写入注册表"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ================================================================== 启动设置
SETTINGS_FILE = os.path.join(HERE, "clash-wifi-gui.json")


def load_settings():
    if not os.path.isfile(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        cur = load_settings()
        cur.update(data)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def elevate_args():
    """按是否打包决定传给新进程的参数。"""
    if getattr(sys, "frozen", False):
        return ""
    return '"%s"' % os.path.abspath(__file__)


def ask_elevate():
    """询问是否提权，返回 (是否提权, 是否记住)。

    直接在根窗口上绘制：若使用已隐藏根窗口的 Toplevel，Windows 不会绘制该窗口。
    """
    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg=C_BG)
    root.resizable(False, False)

    w, h = 560, 320
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    tk.Label(root, text="需要管理员权限", bg=C_BG, fg=C_INK,
             font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", padx=22, pady=(18, 6))
    tk.Label(root, bg=C_BG, fg="#3d4451", justify="left", font=FONT,
             text=("以下功能需要管理员权限：\n"
                   "    设置网卡优先级、断网测试、注册开机自启\n\n"
                   "以普通权限运行，其余检测与配置功能仍可正常使用。")
             ).pack(anchor="w", padx=22)

    remember = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="记住此选择", variable=remember, bg=C_BG,
                   activebackground=C_BG, font=FONT_S).pack(anchor="w", padx=18, pady=(8, 0))

    result = {"yes": False}

    def choose(v):
        result["yes"] = v
        root.quit()

    bar = tk.Frame(root, bg=C_BG)
    bar.pack(side="bottom", fill="x", padx=22, pady=14)
    ttk.Button(bar, text="以管理员身份重启",
               command=lambda: choose(True)).pack(side="right", padx=(8, 0))
    ttk.Button(bar, text="以普通权限运行",
               command=lambda: choose(False)).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", lambda: choose(False))
    root.lift()
    root.attributes("-topmost", True)
    root.mainloop()

    ok = result["yes"]
    memo = bool(remember.get())
    root.destroy()
    return ok, memo

def main():
    argv = sys.argv
    ensure_assets()

    if "--selftest" in argv:
        return selftest()

    ensure_assets()

    pref = load_settings().get("auto_elevate", "ask")
    bypass = "--no-elevate" in argv

    if not is_admin() and not bypass:
        if pref == "yes":
            if elevate():
                return 0
        elif pref == "ask":
            ok, remember = ask_elevate()
            if remember:
                save_settings({"auto_elevate": "yes" if ok else "no"})
            if ok and elevate():
                return 0

    app = App()
    if "--tab" in argv:
        try:
            app.nb.select(int(argv[argv.index("--tab") + 1]))
        except Exception:
            pass
    app.mainloop()
    return 0

# ================================================================== 探测层
def list_adapters():
    out = []
    for line in run(["netsh", "interface", "ipv4", "show", "interfaces"]).splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$", line)
        if not m:
            continue
        state = m.group(4)
        out.append({
            "idx": int(m.group(1)),
            "metric": int(m.group(2)),
            "state": state,
            "name": m.group(5),
            "up": ("dis" not in state.lower()) and bool(
                re.search(r"^connected|已连接", state)),
        })
    return out


def adapter_config(name):
    txt = run(["netsh", "interface", "ipv4", "show", "config", f'name={name}'])
    ip = re.search(r"(?:IP Address|IP 地址)\s*[:：]\s*(\d+\.\d+\.\d+\.\d+)", txt)
    gw = re.search(r"(?:Default Gateway|默认网关)\s*[:：]\s*(\d+\.\d+\.\d+\.\d+)", txt)
    mt = re.search(r"(?:InterfaceMetric|接口度量|接口跃点数)\s*[:：]\s*(\d+)", txt)
    return {"ip": ip.group(1) if ip else None,
            "gw": gw.group(1) if gw else None,
            "metric": int(mt.group(1)) if mt else None}


def default_routes():
    rows, seen, out = [], set(), []
    for line in run(["route", "print", "-4"]).splitlines():
        m = re.match(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+"
                     r"(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s*$", line)
        if m:
            rows.append({"gw": m.group(1), "ip": m.group(2), "metric": int(m.group(3))})
    for r in sorted(rows, key=lambda x: x["metric"]):
        k = (r["gw"], r["metric"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def find_config_dir():
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    for c in [os.path.join(appdata, "io.github.clash-verge-rev.clash-verge-rev"),
              os.path.join(local, "io.github.clash-verge-rev.clash-verge-rev"),
              os.path.join(appdata, "clash-verge"),
              os.path.join(local, "clash-verge")]:
        if c and os.path.isdir(os.path.join(c, "profiles")):
            return c
    for root in (appdata, local):
        if not root or not os.path.isdir(root):
            continue
        try:
            for d in os.listdir(root):
                p = os.path.join(root, d)
                if os.path.isdir(os.path.join(p, "profiles")):
                    return p
        except Exception:
            pass
    return None


def read_kv(path, key, max_lines=5000):
    if not path or not os.path.isfile(path):
        return None, None
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*:\s*(.*)$")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > max_lines:
                    break
                m = pat.match(line)
                if m:
                    return (m.group(1).strip().strip('"').strip("'"),
                            bool(re.match(r"^\s+\S", line)))
    except Exception:
        pass
    return None, None


def clash_procs():
    out = []
    for line in run(["tasklist"]).splitlines():
        m = re.match(r"^\s*(\S+\.exe)\s+(\d+)\s+(\S+)", line)
        if m and re.search(r"mihomo|clash", m.group(1), re.I):
            out.append({"name": m.group(1), "pid": int(m.group(2)), "session": m.group(3)})
    return out


def listener_pid(port):
    for line in run(["netstat", "-ano"]).splitlines():
        if f":{port}" in line and "LISTENING" in line:
            f = line.split()
            if len(f) >= 5:
                try:
                    return int(f[-1])
                except ValueError:
                    pass
    return None


def established():
    rows = []
    for line in run(["netstat", "-ano"]).splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "TCP" and f[3] == "ESTABLISHED":
            src = f[1].rsplit(":", 1)[0]
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", src):
                try:
                    rows.append({"src": src, "remote": f[2], "pid": int(f[4])})
                except ValueError:
                    pass
    return rows


def registry_proxy():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            en = winreg.QueryValueEx(k, "ProxyEnable")[0]
        except OSError:
            en = 0
        try:
            srv = winreg.QueryValueEx(k, "ProxyServer")[0]
        except OSError:
            srv = ""
        winreg.CloseKey(k)
        return int(en), str(srv)
    except Exception:
        return -1, ""


# ================================================================== 诊断层
def diagnose():
    d = {"ts": time.strftime("%H:%M:%S")}
    ads = list_adapters()
    for a in ads:
        c = adapter_config(a["name"])
        a["ip"], a["gw"] = c["ip"], c["gw"]
    d["adapters"] = ads

    wifi = next((a for a in ads if WIFI_RE.search(a["name"])), None)
    eth = next((a for a in ads if not WIFI_RE.search(a["name"])
                and "Loopback" not in a["name"] and a["up"]), None)
    d["wifi"], d["eth"] = wifi, eth

    d["routes"] = default_routes()
    d["winner"] = d["routes"][0] if d["routes"] else None

    cfg = find_config_dir()
    d["cfg_dir"] = cfg
    if cfg:
        d["merge_val"], d["merge_indent"] = read_kv(
            os.path.join(cfg, "profiles", "Merge.yaml"), KEY)
        d["runtime_val"], d["runtime_indent"] = read_kv(
            os.path.join(cfg, "clash-verge.yaml"), KEY)
        d["stack_val"], _ = read_kv(os.path.join(cfg, "config.yaml"), "stack")
        p1, _ = read_kv(os.path.join(cfg, "clash-verge.yaml"), "mixed-port")
        p2, _ = read_kv(os.path.join(cfg, "config.yaml"), "mixed-port")
        sp, _ = read_kv(os.path.join(cfg, "verge.yaml"), "enable_system_proxy")
        tp, _ = read_kv(os.path.join(cfg, "verge.yaml"), "enable_tun_mode")
        d["port"] = p1 or p2 or "7897"
        d["tun"] = (tp or "").lower() == "true"
    else:
        for k in ("merge_val", "runtime_val", "stack_val", "tun"):
            d[k] = None
        d["merge_indent"] = d["runtime_indent"] = None
        d["port"] = "7897"

    d["procs"] = clash_procs()
    d["clash_pid"] = listener_pid(d["port"])
    if d["clash_pid"] and not d["procs"]:
        d["procs"] = [{"name": "mihomo（列表不可见）", "pid": d["clash_pid"], "session": "?"}]
    d["sysproxy"] = registry_proxy()

    checks = []

    def add(name, level, detail):
        checks.append({"name": name, "level": level, "detail": detail})

    if wifi and wifi["up"]:
        add("无线网卡在线", "pass", f"{wifi['name']}，{wifi.get('ip') or '无地址'}")
    elif wifi:
        add("无线网卡在线", "fail", f"{wifi['name']} 未连接。此状态下 Clash 无法出网，属预期行为")
    else:
        add("无线网卡在线", "fail", "未检测到无线网卡")

    if eth and eth["up"]:
        add("有线网卡在线", "pass", f"{eth['name']}，{eth.get('ip') or '无地址'}")
    elif eth:
        add("有线网卡在线", "warn", f"{eth['name']} 未连接。全部流量将回落至无线网卡")
    else:
        add("有线网卡在线", "warn", "未检测到在线的有线网卡")

    if wifi and eth:
        if eth["metric"] < wifi["metric"]:
            add("网卡优先级", "pass", f"有线 {eth['metric']} 优于无线 {wifi['metric']}")
        elif eth["metric"] == wifi["metric"]:
            add("网卡优先级", "fail", f"两者均为 {eth['metric']}，默认出口不确定")
        else:
            add("网卡优先级", "fail", f"无线 {wifi['metric']} 优于有线 {eth['metric']}，其他程序会被导向无线")
    else:
        add("网卡优先级", "skip", "网卡信息不完整，无法比较")

    routes = d["routes"]
    if not routes:
        add("默认路由", "fail", "不存在任何默认路由，整机无法出网")
    elif len(routes) == 1:
        only = routes[0]
        owner = "有线" if eth and only["ip"] == eth.get("ip") else (
            "无线" if wifi and only["ip"] == wifi.get("ip") else "未知")
        add("默认路由", "warn",
            f"仅一条（{owner}，{only['gw']}）。单链路环境无法验证绑定是否生效")
    else:
        add("默认路由", "pass",
            f"{len(routes)} 条，当前生效 {routes[0]['gw']}（metric {routes[0]['metric']}）")

    if wifi:
        has = any(r["ip"] == wifi.get("ip") for r in routes)
        add("无线网卡具备默认路由", "pass" if has else "warn",
            "存在，Clash 可经此出网" if has else "不存在，绑定生效后 Clash 将无法出网")

    mv = d.get("merge_val")
    if mv:
        ok = bool(wifi) and mv == wifi["name"]
        add("配置文件已写入", "pass" if ok else "warn",
            f"interface-name = {mv}" + ("" if ok else f"，与网卡名 {wifi['name'] if wifi else '未知'} 不一致"))
    else:
        add("配置文件已写入", "fail", "Merge.yaml 中没有 interface-name")

    rv, ri = d.get("runtime_val"), d.get("runtime_indent")
    if rv and not ri:
        ok = bool(wifi) and rv == wifi["name"]
        add("已生效于运行配置", "pass" if ok else "warn", f"clash-verge.yaml 中 interface-name = {rv}")
    elif rv and ri:
        add("已生效于运行配置", "fail", f"存在缩进错误（{rv}），内核将忽略该项")
    else:
        add("已生效于运行配置", "fail", "clash-verge.yaml 中无 interface-name，需在 Clash Verge 中重新应用订阅")

    if d["sysproxy"][0] == 1:
        add("流量入口", "pass", f"系统代理已开启（{d['sysproxy'][1] or '未注明地址'}）")
    elif d["tun"]:
        add("流量入口", "pass", "TUN 模式已开启")
    else:
        add("流量入口", "warn", "系统代理与 TUN 均未开启，当前没有流量经过 Clash")

    if d["procs"]:
        names = "、".join(f"{p['name']}({p['pid']})" for p in d["procs"])
        svc = [p for p in d["procs"] if str(p["session"]).lower().startswith("service")]
        add("Clash 进程", "warn" if svc else "pass",
            names + ("。存在服务模式进程，普通权限无法终止" if svc else ""))
    else:
        add("Clash 进程", "warn", "未运行")



    d["checks"] = checks
    lv = [c["level"] for c in checks]
    d["verdict"] = "fail" if "fail" in lv else ("warn" if "warn" in lv else "ok")
    return d


def split_report(d=None):
    d = d or diagnose()
    wifi, eth, cpid = d.get("wifi"), d.get("eth"), d.get("clash_pid")
    out = [f"检测时间 {d['ts']}",
           f"无线  {wifi['name'] if wifi else '未知'}  IP={wifi.get('ip') if wifi else '未知'}"
           f"  metric={wifi['metric'] if wifi else '未知'}",
           f"有线  {eth['name'] if eth else '未知'}  IP={eth.get('ip') if eth else '未知'}"
           f"  metric={eth['metric'] if eth else '未知'}",
           f"Clash 内核 PID = {cpid if cpid else '未找到'}（端口 {d['port']}）", ""]

    if not (wifi and eth and wifi.get("ip") and eth.get("ip")):
        out.append("两条链路未同时具备地址，无法进行分布判定。")
        return "\n".join(out), {"ok": False}

    rows = [r for r in established() if r["src"] in (wifi["ip"], eth["ip"])]
    groups = {}
    for r in rows:
        groups.setdefault(r["src"], []).append(r)

    for ip, label in ((wifi["ip"], "无线"), (eth["ip"], "有线")):
        out.append(f"── {label} {ip} ──")
        lst = groups.get(ip, [])
        if not lst:
            out.append("   无连接")
        else:
            by = {}
            for r in lst:
                by.setdefault(r["pid"], []).append(r["remote"])
            for pid, rem in sorted(by.items(), key=lambda x: -len(x[1])):
                tag = "  [Clash 内核]" if pid == cpid else ""
                out.append(f"   PID {pid:<6} {len(rem):>2} 条{tag}")
                for r in rem[:4]:
                    out.append(f"       {r}")
        out.append("")

    cw = sum(1 for r in groups.get(wifi["ip"], []) if r["pid"] == cpid)
    ce = sum(1 for r in groups.get(eth["ip"], []) if r["pid"] == cpid)
    ow = sum(1 for r in groups.get(wifi["ip"], []) if r["pid"] != cpid)

    out.append("── 判定 ──")
    ok = True
    if cw:
        out.append(f"[通过] Clash 经无线出站 {cw} 条")
    else:
        out.append("[未通过] Clash 无连接经无线出站（未使用，或绑定未生效）")
        ok = False
    if ce:
        out.append(f"[未通过] Clash 经有线出站 {ce} 条，存在泄漏")
        ok = False
    else:
        out.append("[通过] Clash 无连接经有线出站")
    out.append(f"[提示] 其他程序经无线出站 {ow} 条（多为有线中断期间的残留连接）"
               if ow else "[通过] 其他程序均经有线出站")
    return "\n".join(out), {"ok": ok, "clash_on_wifi": cw, "clash_on_eth": ce,
                            "other_on_wifi": ow}


def curl_ip(proxy=None, timeout=10):
    for url in TEST_URLS:
        args = ["curl.exe", "-s", "--max-time", str(timeout)]
        if proxy:
            args += ["-x", proxy]
        args.append(url)
        txt = run(args, timeout=timeout + 5).strip()
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", txt)
        if m:
            return m.group(1)
    return None


# ================================================================== 变更记录
def load_history():
    if not os.path.isfile(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(items):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record(title, detail, undo):
    items = load_history()
    items.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "title": title, "detail": detail, "undo": undo})
    save_history(items)


def apply_undo(u, log):
    t = u.get("t")
    if t == "file":
        path = u.get("path")
        if u.get("created") and os.path.isfile(path):
            os.remove(path)
            log(f"已删除新建文件：{path}")
            return True
        bak = u.get("backup")
        if bak and os.path.isfile(bak):
            shutil.copy2(bak, path)
            log(f"已从备份还原：{os.path.basename(bak)} -> {os.path.basename(path)}")
            return True
        log("未找到备份文件，跳过")
        return False
    if t == "metric":
        for name, old in (u.get("items") or {}).items():
            if old in (None, ""):
                continue
            run(["netsh", "interface", "ipv4", "set", "interface", name, f"metric={old}"])
            log(f"已还原 {name} 的 metric 为 {old}")
        return True
    if t == "proxy":
        ok, err = set_proxy_enable(u.get("old", 0))
        log("系统代理已还原" if ok else f"系统代理还原失败：{err}")
        return ok
    if t == "proc":
        pid = u.get("pid")
        if pid:
            run(["taskkill", "/F", "/PID", str(pid)])
            log(f"已终止进程 {pid}")
        return True
    if t == "task":
        name = u.get("name") or "ClashWatchdog"
        run(["schtasks", "/Delete", "/TN", name, "/F"])
        log(f"已删除计划任务：{name}")
        return True
    log("未知的回退类型")
    return False


def undo_last(log):
    items = load_history()
    if not items:
        return False, "没有可回退的操作"
    last = items.pop()
    apply_undo(last.get("undo", {}), log)
    save_history(items)
    return True, f"已撤销：{last['title']}"


def undo_all(log):
    items = load_history()
    if not items:
        return False, "没有可回退的操作"
    for it in reversed(items):
        log(f"回退：{it['title']}")
        apply_undo(it.get("undo", {}), log)
    save_history([])
    return True, f"已全部还原，共 {len(items)} 项"


# ================================================================== 写配置
def write_interface_name(wifi_name, log):
    cfg = find_config_dir()
    if not cfg:
        return False, "未找到 Clash Verge 配置目录", None
    merge = os.path.join(cfg, "profiles", "Merge.yaml")
    os.makedirs(os.path.dirname(merge), exist_ok=True)

    created = not os.path.isfile(merge)
    backup, text = None, ""
    if not created:
        with open(merge, "rb") as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except Exception:
            return False, "Merge.yaml 非 UTF-8 编码，已中止以避免损坏", None
        backup = merge + "." + time.strftime("%Y%m%d-%H%M%S") + ".bak"
        shutil.copy2(merge, backup)
        log(f"已备份：{os.path.basename(backup)}")

    lines = text.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if re.match(r"^" + re.escape(KEY) + r"\s*:", ln)), None)
    if idx is not None:
        lines[idx] = f"{KEY}: {wifi_name}"
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append("# 指定 Clash 出站绑定的网卡")
        lines.append(f"{KEY}: {wifi_name}")

    with open(merge, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    val, _ = read_kv(merge, KEY)
    if val != wifi_name:
        return False, "写入后回读校验失败", None
    log(f"已写入：{merge}")
    return True, "", {"t": "file", "path": merge, "backup": backup, "created": created}


def set_metrics(eth_name, eth_m, wifi_name, wifi_m, log):
    olds = {a["name"]: a["metric"] for a in list_adapters()
            if a["name"] in (eth_name, wifi_name)}
    for name, m in ((eth_name, eth_m), (wifi_name, wifi_m)):
        run(["netsh", "interface", "ipv4", "set", "interface", f"{name}", f"metric={m}"])
        log(f'netsh set interface "{name}" metric={m}')
    time.sleep(0.4)
    return olds


# ================================================================== 界面
class App(tk.Tk):
    """仪表盘 + 分步引导 + 回退，三种视图并存。"""

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.configure(bg=C_BG)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1300, sw - 60), min(900, sh - 110)
        self.geometry(f"{w}x{h}+30+20")
        self.minsize(1000, 660)

        self.q = queue.Queue()
        self.d = None
        self.watchdog = None
        self.out = {}
        self.card = {}

        self._style()
        self._ui()
        self.update_idletasks()
        self.geometry(f"{w}x{h}+30+20")
        self.after(100, self._pump)
        self.refresh(silent=True)

    # ------------------------------------------------ 样式
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", font=FONT, background=C_BG)
        st.configure("TFrame", background=C_BG)
        st.configure("TLabel", background=C_BG, foreground=C_INK, font=FONT)
        st.configure("Title.TLabel", font=FONT_H, background=C_BG)
        st.configure("Sub.TLabel", font=FONT_S, foreground=C_SKIP, background=C_BG)
        st.configure("TNotebook", background=C_BG, borderwidth=0)
        st.configure("TNotebook.Tab", font=FONT, padding=(15, 7))
        st.configure("TButton", font=FONT_S, padding=(10, 5))
        st.configure("Treeview", font=FONT_S, rowheight=24,
                     background=C_CARD, fieldbackground=C_CARD)
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("TLabelframe", background=C_BG)
        st.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"),
                     background=C_BG)

    # ------------------------------------------------ 主结构
    def _ui(self):
        head = ttk.Frame(self)
        head.pack(fill="x", padx=16, pady=(10, 2))
        ttk.Label(head, text="Clash 出站绑定与路由检测", style="Title.TLabel").pack(side="left")
        self.badge = tk.Label(head, text="检测中", bg=C_SKIP, fg="white",
                              font=("Microsoft YaHei UI", 9, "bold"), padx=12, pady=4)
        self.badge.pack(side="right")
        self.role = ttk.Label(head, style="Sub.TLabel",
                              text="管理员权限" if is_admin()
                              else "普通权限：修改 metric 与断网测试需管理员")
        self.role.pack(side="right", padx=12)

        # 全局日志：固定在底部
        logf = ttk.LabelFrame(self, text=" 运行日志 ")
        logf.pack(side="bottom", fill="x", padx=16, pady=(2, 10))
        self.txt_log = tk.Text(logf, height=5, wrap="word", font=FONT_MONO,
                               bg=C_CARD, fg="#3d4451", relief="flat", padx=8, pady=5)
        sb = ttk.Scrollbar(logf, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=4)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=16, pady=2)

        self._tab_dashboard()
        self._tab_step1()
        self._tab_step2()
        self._tab_step3()
        self._tab_step4()
        self._tab_step5()
        self._tab_rollback()
        self._tab_about()

    def _page(self, title):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text=title)
        return f

    # ------------------------------------------------ 仪表盘
    def _card(self, parent, col, title):
        c = tk.Frame(parent, bg=C_CARD, highlightthickness=1, highlightbackground="#d6dbe2")
        c.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
        parent.columnconfigure(col, weight=1)
        tk.Label(c, text=title, bg=C_CARD, fg=C_SKIP,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=12, pady=(7, 0))
        v = tk.Label(c, text="—", bg=C_CARD, fg=C_INK,
                     font=("Microsoft YaHei UI", 11, "bold"), justify="left")
        v.pack(anchor="w", padx=12, pady=(1, 8))
        return v

    def _tab_dashboard(self):
        f = self._page("仪表盘")

        cards = ttk.Frame(f)
        cards.pack(fill="x", pady=(10, 6))
        self.card["wifi"] = self._card(cards, 0, "无线出口")
        self.card["eth"] = self._card(cards, 1, "有线出口")
        self.card["route"] = self._card(cards, 2, "生效默认路由")
        self.card["bind"] = self._card(cards, 3, "Clash 绑定")

        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True)
        mid.columnconfigure(0, weight=4)
        mid.columnconfigure(1, weight=6)
        mid.rowconfigure(0, weight=1)

        lf1 = ttk.LabelFrame(mid, text=" 网卡与默认路由 ")
        lf1.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        cols = ("idx", "name", "state", "metric", "ip", "route")
        self.tv_ad = ttk.Treeview(lf1, columns=cols, show="headings", height=6)
        for c, t, wd in zip(cols, ("索引", "网卡", "状态", "metric", "IPv4", "默认路由"),
                            (46, 132, 62, 62, 120, 108)):
            self.tv_ad.heading(c, text=t)
            self.tv_ad.column(c, width=wd, anchor="w")
        self.tv_ad.pack(fill="both", expand=True, padx=5, pady=5)
        self.tv_ad.tag_configure("wifi", background="#eef6ff")
        self.tv_ad.tag_configure("eth", background="#f0fff4")

        lf2 = ttk.LabelFrame(mid, text=" 检查清单（路由与绑定是否设置成功） ")
        lf2.grid(row=0, column=1, sticky="nsew")
        cols2 = ("s", "item", "detail")
        self.tv_ck = ttk.Treeview(lf2, columns=cols2, show="headings", height=6)
        self.tv_ck.heading("s", text="")
        self.tv_ck.heading("item", text="检查项")
        self.tv_ck.heading("detail", text="说明")
        self.tv_ck.column("s", width=52, anchor="center")
        self.tv_ck.column("item", width=150, anchor="w")
        self.tv_ck.column("detail", width=430, anchor="w")
        self.tv_ck.pack(fill="both", expand=True, padx=5, pady=5)
        for lv, col in (("pass", C_OK), ("warn", C_WARN), ("fail", C_BAD), ("skip", C_SKIP)):
            self.tv_ck.tag_configure(lv, foreground=col)

        lf3 = ttk.LabelFrame(f, text=" 出口分布（依据连接源地址，无需联网） ")
        lf3.pack(fill="both", expand=True, pady=(8, 0))
        self.txt_split = tk.Text(lf3, wrap="none", font=FONT_MONO, height=8,
                                 bg="#0d1117", fg="#c9d1d9", insertbackground="#c9d1d9",
                                 relief="flat", padx=10, pady=6)
        sb = ttk.Scrollbar(lf3, command=self.txt_split.yview)
        self.txt_split.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.txt_split.pack(fill="both", expand=True, padx=5, pady=5)
        self._set_split("点击下方「出口分布」查看 Clash 与其他程序各自的出站链路。")

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(8, 4))
        for text, cb in [
            ("重新检测", self.refresh),
            ("写入 interface-name", self.act_write),
            ("设置网卡优先级", self.act_metric),
            ("出口分布", self.act_split),
            ("断网测试", self.act_drill),
            ("看门狗 开/关", self.act_watchdog_toggle),
            ("打开配置目录", self.act_open_cfg),
            ("复制报告", self.act_copy),
        ]:
            ttk.Button(bar, text=text, command=cb).pack(side="left", padx=(0, 7))

        # 重新打包：让按钮栏优先占据底部空间，避免被上方扩展面板挤压
        bar.pack_forget()
        bar.pack(side="bottom", fill="x", pady=(8, 4), before=mid)

    # ------------------------------------------------ 分步页
    def _doc(self, parent, headline, why, steps, ok):
        ttk.Label(parent, text=headline, style="Title.TLabel").pack(anchor="w", pady=(12, 4))
        box = tk.Frame(parent, bg=C_CARD, highlightthickness=1, highlightbackground="#d6dbe2")
        box.pack(fill="x", pady=(2, 0))
        body = []
        if why:
            body.append("为什么要做这一步\n" + why)
        if steps:
            body.append("操作步骤\n" + "\n".join(
                f"    {i}. {s}" for i, s in enumerate(steps, 1)))
        if ok:
            body.append("成功标志\n" + ok)
        txt = tk.Text(box, wrap="word", font=FONT, bg=C_CARD, fg=C_INK,
                      relief="flat", padx=12, pady=9)
        txt.insert("1.0", "\n\n".join(body))
        txt.configure(state="disabled")
        txt.pack(fill="x")
        # 控件此处尚未完成布局，直接取 displaylines 会严重失真，改为按字符数估算
        est = 0
        for seg in "\n\n".join(body).split("\n"):
            est += max(1, -(-len(seg) // 78))
        txt.configure(height=max(6, min(est + 2, 20)))

    def _btns(self, parent, items):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(9, 0))
        for text, cb in items:
            ttk.Button(bar, text=text, command=cb).pack(side="left", padx=(0, 7))
        return bar

    def _outbox(self, parent, name, height=8, light=False):
        fr = ttk.LabelFrame(parent, text=" 输出 ")
        fr.pack(fill="both", expand=True, pady=(9, 0))
        kw = dict(bg=C_CARD, fg=C_INK) if light else dict(bg="#0d1117", fg="#c9d1d9")
        t = tk.Text(fr, height=height, wrap="none", font=FONT_MONO,
                    relief="flat", padx=8, pady=6, **kw)
        sb = ttk.Scrollbar(fr, command=t.yview)
        t.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True, padx=5, pady=5)
        self.out[name] = t
        return t

    def _tab_step1(self):
        f = self._page("步骤一 定位")
        self._doc(f, "定位 Clash 的安装路径与配置目录",
                  "后续步骤需向 Merge.yaml 写入配置，并可能对内核设置防火墙规则，"
                  "因此需先确定程序位置。不同设备安装位置不同，采用多来源自动查找。",
                  ["点击「自动定位」。",
                   "若未找到，点击「从桌面图标选择」，在列表中选择 Clash 快捷方式。",
                   "确认输出的 GUI 路径、内核路径、配置目录三项均已识别。"],
                  "GUI 与内核路径均指向真实存在的文件，配置目录下存在 profiles 子目录。")
        self._btns(f, [("自动定位", self.act_locate),
                       ("从桌面图标选择", self.act_locate_pick),
                       ("打开配置目录", self.act_open_cfg)])
        self._outbox(f, "locate", 9)

    def _tab_step2(self):
        f = self._page("步骤二 检测")
        self._doc(f, "检测网卡、路由与配置状态",
                  "在修改任何设置前先确认当前状态，避免在错误的基线上操作。"
                  "检测项覆盖网卡连通性、默认路由归属、网卡优先级、配置生效情况与进程状态。",
                  ["点击「开始检测」。",
                   "查看检查清单，失败项排在最前。",
                   "按提示逐项处理，或进入步骤三执行配置。"],
                  "清单中不再出现失败项；存在警告项时按提示判断是否影响使用。")
        self._btns(f, [("开始检测", self.refresh),
                       ("复制检测报告", self.act_copy),
                       ("以管理员身份重启", self._elevate)])
        self._outbox(f, "check", 11, light=True)

    def _tab_step3(self):
        f = self._page("步骤三 配置")
        self._doc(f, "写入配置并设置网卡优先级",
                  "写入 interface-name 使 Clash 仅从无线网卡出站；"
                  "调整 metric 使其他程序优先使用有线网卡。两项操作均可回退。",
                  ["点击「写入 interface-name」，确认后程序自动备份并写入。",
                   "返回 Clash Verge，点击当前订阅，使其重新生成运行配置。",
                   "点击「设置网卡优先级」（需管理员权限），有线设为 10，无线设为 9999。",
                   "回到步骤二重新检测，确认「已生效于运行配置」为通过。"],
                  "Merge.yaml 与 clash-verge.yaml 中均存在 interface-name，且取值与无线网卡名一致。")
        self._btns(f, [("写入 interface-name", self.act_write),
                       ("设置网卡优先级", self.act_metric),
                       ("撤销上一步", self.act_undo),
                       ("打开配置目录", self.act_open_cfg)])
        self._outbox(f, "config", 8)

    def _tab_step4(self):
        f = self._page("步骤四 验证")
        self._doc(f, "验证出口分布与失效行为",
                  "配置是否真正生效，只能通过观察实际连接判断。"
                  "出口分布依据 TCP 连接的源地址，无需联网即可得出结论。",
                  ["点击「出口分布」，确认 Clash 的连接经无线出站，且无连接经有线出站。",
                   "点击「断网测试」（需管理员权限），程序将断开无线、复测、自动重连。",
                   "断网期间请求应全部失败；若仍返回地址，说明流量已回落至有线。"],
                  "出口分布显示 Clash 经无线出站、经有线出站为 0；断网测试结果为请求失败。")
        self._btns(f, [("出口分布", self.act_split),
                       ("断网测试", self.act_drill),
                       ("重新检测", self.refresh)])
        self._outbox(f, "verify", 10)

    def _tab_step5(self):
        f = self._page("步骤五 看门狗")
        self._doc(f, "处理无线断开后的副作用",
                  "interface-name 全局生效，无线断开时经 Clash 的请求全部失败；"
                  "若系统代理仍开启，浏览器将无法访问任何站点。"
                  "看门狗在检测到无线断开时关闭系统代理并终止 Clash。",
                  ["点击「启动看门狗」，程序在新窗口中运行监控。",
                   "点击「停止看门狗」结束监控。",
                   "如需无线恢复后自动还原，改用命令行：clash-watchdog.cmd -RestoreOnUp。"],
                  "日志中出现「系统代理已关闭」等记录；无线断开后浏览器可正常访问国内站点。\n\n"
                  "持久性\n"
                  "    interface-name 与网卡 metric 写入磁盘与系统配置，重启后仍然生效。\n"
                  "    看门狗为独立进程，需注册开机自启后才能随系统启动。")
        self._btns(f, [("启动看门狗", self.act_watchdog_start),
                       ("停止看门狗", self.act_watchdog_stop),
                       ("注册开机自启", self.act_task_install),
                       ("取消开机自启", self.act_task_remove),
                       ("打开看门狗日志", self.act_open_wd_log)])
        self.lb_task = ttk.Label(f, text="", style="Sub.TLabel")
        self.lb_task.pack(anchor="w", pady=(8, 0))
        self.act_task_status()

        self._outbox(f, "wd", 8)

    def _tab_rollback(self):
        f = self._page("回退与还原")
        self._doc(f, "撤销此前的变更",
                  "本程序对系统的每一次修改都会记录原始值：配置文件备份路径、网卡 metric、"
                  "系统代理开关、看门狗进程。可撤销最近一次操作，或一次性全部还原。",
                  ["查看下方变更记录，确认需要撤销的条目。",
                   "点击「撤销最近一次」，按时间倒序逐条回退。",
                   "或点击「全部还原」，一次性回退全部记录。",
                   "如已手工改动配置，可从 Merge.yaml 同目录的 .bak 备份恢复。"],
                  "变更记录清空；步骤二的检测结果恢复到此前的状态。")
        bar_rb = self._btns(f, [("刷新记录", self.act_hist_refresh),
                                ("撤销最近一次", self.act_undo),
                                ("全部还原", self.act_undo_all),
                                ("打开备份目录", self.act_open_cfg)])

        fr = ttk.LabelFrame(f, text=" 变更记录 ")
        fr.pack(fill="both", expand=True, pady=(9, 0))
        cols = ("time", "title", "detail")
        self.tv_hist = ttk.Treeview(fr, columns=cols, show="headings", height=8)
        for c, t, wd in zip(cols, ("时间", "操作", "详情"), (150, 190, 640)):
            self.tv_hist.heading(c, text=t)
            self.tv_hist.column(c, width=wd, anchor="w")
        sb = ttk.Scrollbar(fr, command=self.tv_hist.yview)
        self.tv_hist.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tv_hist.pack(fill="both", expand=True, padx=5, pady=5)
        self.bar_rb = bar_rb
        bar_rb.pack_forget()
        bar_rb.pack(side="bottom", fill="x", pady=(9, 0), before=fr)
        self.act_hist_refresh()

    def _tab_about(self):
        f = self._page("关于")
        ttk.Label(f, text=f"{APP_NAME} {APP_VERSION}", style="Title.TLabel").pack(anchor="w", pady=(14, 4))
        ttk.Label(f, style="Sub.TLabel",
                  text="用于配置与检测 Clash 出站网卡绑定的 Windows 工具").pack(anchor="w")

        body = (
            "功能\n"
            "    自动定位 Clash Verge 安装路径、内核与配置目录\n"
            "    检测网卡、路由、配置项是否设置成功\n"
            "    写入 interface-name，设置网卡优先级\n"
            "    验证出口分布，执行断网测试\n"
            "    管理看门狗进程\n"
            "    记录变更并支持撤销与全部还原\n"
            "\n"
            "依赖\n"
            "    Windows 10 / 11\n"
            "    Clash Verge Rev 与 mihomo 内核\n"
            "    Python 3.9 及以上（仅图形界面需要）\n"
            "\n"
            "声明\n"
            "    本工具仅修改本机网络配置与 Clash 配置文件。\n"
            "    所有写操作在修改前生成备份并记入变更历史，可逐项撤销。\n"
            "    使用前请确认对设备的配置权限。"
        )
        box = tk.Frame(f, bg=C_CARD, highlightthickness=1, highlightbackground="#d6dbe2")
        box.pack(fill="x", pady=(10, 0))
        t = tk.Text(box, wrap="word", font=FONT, bg=C_CARD, fg=C_INK,
                    relief="flat", height=15, padx=12, pady=9)
        t.insert("1.0", body)
        t.configure(state="disabled")
        t.pack(fill="x")

        link = tk.Frame(f, bg=C_BG)
        link.pack(side="bottom", anchor="w", pady=(10, 6))
        ttk.Label(link, text="站点：", font=FONT_S).pack(side="left")
        lb = tk.Label(link, text=SITE_URL, fg="#0969da", bg=C_BG,
                      font=("Microsoft YaHei UI", 9, "underline"), cursor="hand2")
        lb.pack(side="left")
        lb.bind("<Button-1>", lambda e: webbrowser.open(SITE_URL))
        ttk.Label(link, text=f"（{SITE_NAME}）", style="Sub.TLabel").pack(side="left", padx=(6, 0))

        pref = tk.Frame(f, bg=C_BG)
        pref.pack(side="bottom", anchor="w", pady=(4, 6))
        ttk.Label(pref, text="启动权限：", font=FONT_S).pack(side="left")
        self.lb_pref = ttk.Label(pref, text="", style="Sub.TLabel")
        self.lb_pref.pack(side="left")
        ttk.Button(pref, text="恢复为每次询问",
                   command=self.act_pref_reset).pack(side="left", padx=(10, 0))
        self.act_pref_show()
        self.lb_site = lb

    # ------------------------------------------------ 消息泵
    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "diag":
                    self._render(payload)
                elif kind == "out":
                    self._set_out(*payload)
                elif kind == "split":
                    self._set_split(payload)
                elif kind == "msg":
                    messagebox.showinfo(APP_NAME, payload)
                elif kind == "err":
                    messagebox.showerror(APP_NAME, payload)
                elif kind == "hist":
                    self.act_hist_refresh()
                elif kind == "task":
                    self.act_task_status()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def log(self, s):
        line = f"[{time.strftime('%H:%M:%S')}] {s}\n"
        self.txt_log.insert("end", line)
        self.txt_log.see("end")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _set_out(self, name, text):
        t = self.out.get(name)
        if not t:
            return
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("1.0", text)
        t.configure(state="disabled")

    def _set_split(self, text):
        self.txt_split.configure(state="normal")
        self.txt_split.delete("1.0", "end")
        self.txt_split.insert("1.0", text)
        self.txt_split.configure(state="disabled")

    def _elevate(self):
        if is_admin():
            messagebox.showinfo(APP_NAME, "当前已是管理员权限")
        elif elevate():
            self.destroy()
        else:
            messagebox.showerror(APP_NAME, "提权失败")

    def _bg(self, fn):
        def wrap():
            try:
                fn()
            except Exception as e:
                self.q.put(("err", f"{type(e).__name__}: {e}"))
        threading.Thread(target=wrap, daemon=True).start()

    # ------------------------------------------------ 渲染
    def _render(self, d):
        self.d = d
        text, color = {"ok": ("配置正确", C_OK),
                       "warn": ("存在警告", C_WARN),
                       "fail": ("存在问题", C_BAD)}[d["verdict"]]
        self.badge.configure(text=text, bg=color)

        wifi, eth, win = d.get("wifi"), d.get("eth"), d.get("winner")
        self.card["wifi"].configure(
            text=(f"{wifi['name']}  {wifi.get('ip') or '无地址'}\nmetric {wifi['metric']}"
                  if wifi else "未找到"),
            fg=C_OK if wifi and wifi["up"] else C_BAD)
        self.card["eth"].configure(
            text=(f"{eth['name']}  {eth.get('ip') or '无地址'}\nmetric {eth['metric']}"
                  if eth else "未找到"),
            fg=C_OK if eth and eth["up"] else C_WARN)
        self.card["route"].configure(
            text=(f"{win['gw']}\nmetric {win['metric']}" if win else "无默认路由"),
            fg=C_INK)

        rv, ri = d.get("runtime_val"), d.get("runtime_indent")
        if rv and not ri:
            self.card["bind"].configure(text=f"interface-name\n{rv}", fg=C_OK)
        elif rv and ri:
            self.card["bind"].configure(text="缩进错误\n内核将忽略", fg=C_BAD)
        else:
            self.card["bind"].configure(text="未生效\n未写入运行配置", fg=C_BAD)

        # 网卡表
        for i in self.tv_ad.get_children():
            self.tv_ad.delete(i)
        rmap = {}
        for r in d["routes"]:
            rmap.setdefault(r["ip"], []).append(r)
        for a in d["adapters"]:
            tag = "wifi" if wifi and a["name"] == wifi["name"] else (
                "eth" if eth and a["name"] == eth["name"] else "")
            low = a["state"].lower()
            st_txt = "已连接" if a["up"] else ("已断开" if "dis" in low else a["state"])
            rts = rmap.get(a.get("ip"), [])
            self.tv_ad.insert("", "end", tags=(tag,) if tag else (), values=(
                a["idx"], a["name"], st_txt, a["metric"], a.get("ip") or "",
                ", ".join(f"{x['gw']}(m{x['metric']})" for x in rts) or "—"))

        # 检查清单
        for i in self.tv_ck.get_children():
            self.tv_ck.delete(i)
        sev = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
        mark = {"pass": "✔", "warn": "!", "fail": "✘", "skip": "–"}
        for c in sorted(d["checks"], key=lambda x: sev[x["level"]]):
            self.tv_ck.insert("", "end", tags=(c["level"],),
                              values=(mark[c["level"]], c["name"], c["detail"]))

        # 步骤二的文本清单
        if "check" in self.out:
            ml = {"pass": "[通过]", "warn": "[警告]", "fail": "[失败]", "skip": "[跳过]"}
            lines = [f"{ml[c['level']]} {c['name']}：{c['detail']}"
                     for c in sorted(d["checks"], key=lambda x: sev[x["level"]])]
            lines += ["", f"检测时间 {d['ts']}"]
            self._set_out("check", "\n".join(lines))

    def act_hist_refresh(self):
        for i in self.tv_hist.get_children():
            self.tv_hist.delete(i)
        items = load_history()
        for it in reversed(items):
            self.tv_hist.insert("", "end", values=(it["time"], it["title"], it["detail"]))
        if not items:
            self.tv_hist.insert("", "end", values=("—", "无记录", "尚未进行任何修改"))

    # ------------------------------------------------ 动作
    def refresh(self, silent=False):
        def job():
            if not silent:
                self.q.put(("log", "开始检测"))
            d = diagnose()
            self.q.put(("diag", d))
            if not silent:
                self.q.put(("log", f"检测完成，共 {len(d['checks'])} 项，判定 {d['verdict']}"))
        self._bg(job)

    def act_locate(self, pick=False):
        def job():
            self.q.put(("log", "执行自动定位"))
            finder = os.path.join(HERE, "Find-Clash.ps1")
            if os.path.isfile(finder):
                args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", finder, "-Json"]
                if pick:
                    args.append("-Pick")
                txt = run(args, timeout=180)
                try:
                    info = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
                except Exception:
                    info = None
                if info:
                    self.q.put(("out", ("locate",
                        f"GUI  : {info.get('GuiExe') or '未找到'}\n"
                        f"内核 : {info.get('CoreExe') or '未找到'}\n"
                        f"配置 : {info.get('ConfigDir') or '未找到'}\n"
                        f"Merge: {info.get('MergeYaml') or '未找到'}")))
                    self.q.put(("log", "定位完成"))
                    return
            cfg = find_config_dir()
            self.q.put(("out", ("locate",
                f"GUI  : 未找到\n内核 : 未找到\n配置 : {cfg or '未找到'}")))
            self.q.put(("log", "定位完成：仅识别到配置目录"))
        self._bg(job)

    def act_locate_pick(self):
        self.act_locate(pick=True)

    def _wifi(self):
        if not self.d or not self.d.get("wifi"):
            messagebox.showwarning(APP_NAME, "未检测到无线网卡，请先执行检测")
            return None
        return self.d["wifi"]

    def act_write(self):
        wifi = self._wifi()
        if not wifi:
            return
        cfg = self.d.get("cfg_dir") or ""
        target = os.path.join(cfg, "profiles", "Merge.yaml") if cfg else "未找到"
        if not messagebox.askyesno(
                APP_NAME,
                f"即将写入以下内容：\n\n    {KEY}: {wifi['name']}\n\n"
                f"文件：{target}\n"
                "写入前将自动备份，可在「回退与还原」页撤销。\n\n确认继续？"):
            return

        def job():
            ok, msg, undo = write_interface_name(
                wifi["name"], lambda s: self.q.put(("log", s)))
            if ok:
                record("写入 interface-name", f"{KEY}: {wifi['name']}", undo)
                self.q.put(("out", ("config",
                    f"写入成功：{KEY}: {wifi['name']}\n"
                    "请返回 Clash Verge 点击当前订阅以应用。")))
                self.q.put(("msg", "写入成功。请返回 Clash Verge 点击当前订阅，使其重新生成运行配置。"))
            else:
                self.q.put(("err", f"写入失败：{msg}"))
            self.q.put(("hist", None))
            self.refresh(silent=True)
        self._bg(job)

    def act_metric(self):
        if not self.d or not self.d.get("wifi") or not self.d.get("eth"):
            messagebox.showwarning(APP_NAME, "需同时检测到有线与无线网卡")
            return
        if not is_admin():
            if messagebox.askyesno(APP_NAME, "修改 metric 需要管理员权限，是否立即提权重启？"):
                self._elevate()
            return
        eth, wifi = self.d["eth"], self.d["wifi"]
        if not messagebox.askyesno(
                APP_NAME,
                f"将设置：\n\n    {eth['name']} → metric 10\n    {wifi['name']} → metric 9999\n\n"
                "原始值会被记录，可在「回退与还原」页撤销。确认继续？"):
            return

        def job():
            olds = set_metrics(eth["name"], 10, wifi["name"], 9999,
                               lambda s: self.q.put(("log", s)))
            record("设置网卡优先级", f"有线 10，无线 9999（原值 {olds}）",
                   {"t": "metric", "items": olds})
            self.q.put(("out", ("config",
                f"已设置：\n    有线 metric = 10\n    无线 metric = 9999\n\n原值：{olds}")))
            self.q.put(("msg", "设置完成，建议重新检测以确认结果。"))
            self.q.put(("hist", None))
            self.refresh(silent=True)
        self._bg(job)

    def act_split(self):
        def job():
            self.q.put(("log", "统计出口分布"))
            txt, info = split_report(self.d)
            self.q.put(("split", txt))
            self.q.put(("out", ("verify", txt)))
            self.q.put(("log", f"出口分布判定：{info}"))
        self._bg(job)

    def act_drill(self):
        if not self.d:
            return
        if not is_admin():
            if messagebox.askyesno(APP_NAME, "断网测试需要管理员权限，是否立即提权重启？"):
                self._elevate()
            return
        wifi, port = self.d.get("wifi"), self.d.get("port")
        if not wifi:
            messagebox.showwarning(APP_NAME, "未检测到无线网卡")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "该操作会断开无线网络约 20 秒，随后自动重连。\n\n"
                "判定依据：断开期间经 Clash 的请求应全部失败。\n"
                "若仍可取得地址，说明流量已回落至有线链路。\n\n确认执行？"):
            return

        def job():
            proxy = f"http://127.0.0.1:{port}"
            lines = [f"代理地址 {proxy}", "", "① 断开前基线"]
            before = curl_ip(proxy)
            lines.append(f"   出口地址 {before}")
            if not before:
                lines += ["", "结论：代理当前不可用，无法执行测试。"]
                self.q.put(("out", ("verify", "\n".join(lines))))
                self.q.put(("msg", "代理当前不可用，请先确认 Clash 正常运行并开启系统代理。"))
                return

            lines += ["", "② 断开无线网络"]
            run(["netsh", "wlan", "disconnect", f"interface={wifi['name']}"])
            time.sleep(4)
            lines.append("③ 复测")
            during = curl_ip(proxy, timeout=8)
            lines.append(f"   出口地址 {during if during else '（请求失败）'}")

            lines += ["", "④ 恢复无线网络"]
            for _ in range(5):
                run(["netsh", "wlan", "connect", f"name={wifi['name']}",
                     f"ssid={wifi['name']}", f"interface={wifi['name']}"])
                time.sleep(3)
                st = next((a for a in list_adapters() if a["name"] == wifi["name"]), None)
                if st and st["up"]:
                    lines.append("   已恢复连接")
                    break
            else:
                lines.append("   自动重连未成功，请手动连接无线网络")

            lines += ["", "结论"]
            if during is None:
                lines.append("   通过：断开无线后请求全部失败，未回落至有线链路。")
                verdict = "断网测试通过：无线断开后请求全部失败。"
            elif during == before:
                lines.append("   未通过：断开后仍返回相同地址，可能未真正断开。")
                verdict = "断网测试未通过：断开后仍返回相同地址。"
            else:
                lines.append(f"   未通过：断开后返回不同地址 {during}，流量已回落至其他链路。")
                verdict = f"断网测试未通过：流量已回落至其他链路（{during}）。"
            self.q.put(("out", ("verify", "\n".join(lines))))
            self.q.put(("msg", verdict))
            self.refresh(silent=True)
        self._bg(job)

    def act_watchdog_toggle(self):
        if self.watchdog and self.watchdog.poll() is None:
            self.act_watchdog_stop()
        else:
            self.act_watchdog_start()

    def act_watchdog_start(self):
        script = os.path.join(HERE, "clash-watchdog.ps1")
        if not os.path.isfile(script):
            messagebox.showerror(APP_NAME, "未找到 clash-watchdog.ps1，应与本程序位于同一目录")
            return
        if self.watchdog and self.watchdog.poll() is None:
            messagebox.showinfo(APP_NAME, "看门狗已在运行")
            return
        self.watchdog = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script])
        record("启动看门狗", f"PID {self.watchdog.pid}", {"t": "proc", "pid": self.watchdog.pid})
        self.act_hist_refresh()
        self._set_out("wd", f"看门狗已启动，PID {self.watchdog.pid}\n日志：clash-watchdog.log")
        self.log(f"看门狗已启动（PID {self.watchdog.pid}）")

    def act_watchdog_stop(self):
        if not (self.watchdog and self.watchdog.poll() is None):
            messagebox.showinfo(APP_NAME, "看门狗未在运行")
            return
        run(["taskkill", "/F", "/PID", str(self.watchdog.pid)])
        self.log(f"已停止看门狗（PID {self.watchdog.pid}）")
        self.watchdog = None
        self._set_out("wd", "看门狗已停止")

    def _task_query(self):
        out = run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"], timeout=25)
        if TASK_NAME not in out:
            return None
        m = re.search(r"^\s*(?:状态|Status)\s*[:：]\s*(.+?)\s*$", out, re.M)
        p2 = re.search(r"^\s*(?:下次运行时间|Next Run Time)\s*[:：]\s*(.+?)\s*$", out, re.M)
        return {"state": m.group(1) if m else "未知",
                "next": p2.group(1) if p2 else ""}

    def act_task_status(self):
        info = self._task_query()
        if info:
            txt = f"开机自启：已注册（状态 {info['state']}）"
            if info.get("next"):
                txt += f"，下次运行 {info['next']}"
            self.lb_task.configure(text=txt, foreground=C_OK)
        else:
            self.lb_task.configure(text="开机自启：未注册（重启后看门狗不会自动运行）",
                                   foreground=C_WARN)

    def _ensure_autostart_cmd(self):
        body = (
            "@echo off\r\n"
            "rem 由 Clash WiFi Kit 生成的看门狗启动脚本\r\n"
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden "
            "-File \"%~dp0clash-watchdog.ps1\"\r\n"
        )
        with open(AUTOSTART_CMD, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        return AUTOSTART_CMD

    def act_task_install(self):
        script = os.path.join(HERE, "clash-watchdog.ps1")
        if not os.path.isfile(script):
            messagebox.showerror(APP_NAME, "未找到 clash-watchdog.ps1")
            return
        if not is_admin():
            if messagebox.askyesno(
                    APP_NAME,
                    "注册开机自启需要管理员权限。\n\n"
                    "将以管理员身份重启本程序，重启后再次点击该按钮即可。\n\n是否继续？"):
                self._elevate()
            return
        if not messagebox.askyesno(
                APP_NAME,
                "将注册计划任务：\n\n"
                f"    名称：{TASK_NAME}\n"
                "    触发：用户登录时\n"
                "    权限：最高\n\n"
                "登录后以隐藏窗口运行看门狗，无线断开时自动关闭系统代理并退出 Clash。\n\n确认继续？"):
            return

        def job():
            cmd = self._ensure_autostart_cmd()
            self.q.put(("log", f"生成启动脚本：{cmd}"))
            out = run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", f'"{cmd}"',
                       "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"], timeout=60)
            info = self._task_query()
            if info:
                record("注册开机自启", f"计划任务 {TASK_NAME}", {"t": "task", "name": TASK_NAME})
                self.q.put(("msg",
                            f"注册成功。\n\n状态：{info['state']}\n"
                            f"下次运行：{info.get('next') or '用户登录时'}"))
                self.q.put(("hist", None))
                self.q.put(("task", None))
                self.q.put(("log", f"已注册计划任务 {TASK_NAME}"))
            else:
                self.q.put(("err", "注册失败。\n\n" + out.strip()[:500]))
        self._bg(job)

    def act_task_remove(self):
        if not messagebox.askyesno(APP_NAME, f"将删除计划任务 {TASK_NAME}，确认继续？"):
            return

        def job():
            out = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=30)
            try:
                if os.path.isfile(AUTOSTART_CMD):
                    os.remove(AUTOSTART_CMD)
            except Exception:
                pass
            if self._task_query() is None:
                self.q.put(("msg", "已删除计划任务，开机自启已关闭。"))
                self.q.put(("log", f"已删除计划任务 {TASK_NAME}"))
            else:
                self.q.put(("err", "删除失败。\n\n" + out.strip()[:300]))
            self.q.put(("task", None))
        self._bg(job)
    def act_open_wd_log(self):
        p = os.path.join(HERE, "clash-watchdog.log")
        if os.path.isfile(p):
            os.startfile(p)
        else:
            messagebox.showinfo(APP_NAME, "尚无日志文件")

    def act_open_cfg(self):
        if self.d and self.d.get("cfg_dir"):
            os.startfile(self.d["cfg_dir"])
        else:
            messagebox.showinfo(APP_NAME, "未找到配置目录")

    def act_undo(self):
        if not messagebox.askyesno(APP_NAME, "将撤销最近一次变更，确认继续？"):
            return

        def job():
            ok, msg = undo_last(lambda s: self.q.put(("log", s)))
            self.q.put(("msg", msg))
            self.q.put(("hist", None))
            self.refresh(silent=True)
        self._bg(job)

    def act_undo_all(self):
        if not load_history():
            messagebox.showinfo(APP_NAME, "没有可回退的记录")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "将按时间倒序回退全部变更：恢复 Merge.yaml 备份、还原网卡 metric、"
                "还原系统代理开关、终止看门狗。确认继续？"):
            return

        def job():
            ok, msg = undo_all(lambda s: self.q.put(("log", s)))
            self.q.put(("msg", msg + "\n\n提示：若已修改过订阅，请返回 Clash Verge 重新应用以刷新运行配置。"))
            self.q.put(("hist", None))
            self.refresh(silent=True)
        self._bg(job)

    def act_pref_show(self):
        pref = load_settings().get("auto_elevate", "ask")
        txt = {"ask": "启动时询问",
               "yes": "启动时自动提权",
               "no": "从不提权"}.get(pref, "启动时询问")
        if hasattr(self, "lb_pref"):
            self.lb_pref.configure(text=txt)

    def act_pref_reset(self):
        save_settings({"auto_elevate": "ask"})
        self.act_pref_show()
        messagebox.showinfo(APP_NAME, "已恢复为启动时询问。下次启动生效。")

    def act_copy(self):
        if not self.d:
            return
        sev = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
        lines = [f"{APP_NAME} 检测报告  {self.d['ts']}", ""]
        for c in sorted(self.d["checks"], key=lambda x: sev[x["level"]]):
            lines.append(f"[{c['level'].upper():4}] {c['name']}：{c['detail']}")
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self.log("检测报告已复制到剪贴板")
        messagebox.showinfo(APP_NAME, "检测报告已复制到剪贴板")


# ================================================================== 入口
def selftest():
    # windowed 打包后没有控制台，sys.stdout 为 None，此时写入文件
    if sys.stdout is None:
        try:
            sys.stdout = open(os.path.join(HERE, "clash-wifi-selftest.txt"),
                              "w", encoding="utf-8")
        except Exception:
            return 1
    d = diagnose()
    print(f"判定：{d['verdict']}")
    print(f"无线：{d['wifi']}")
    print(f"有线：{d['eth']}")
    print(f"路由：{d['routes']}")
    print(f"端口：{d['port']}  监听 PID：{d['clash_pid']}")
    print(f"系统代理：{d['sysproxy']}")
    print()
    sev = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
    for c in sorted(d["checks"], key=lambda x: sev[x["level"]]):
        print(f"  [{c['level']:4}] {c['name']:10} {c['detail']}")
    print()
    txt, info = split_report(d)
    print(txt)
    print(json.dumps(info, ensure_ascii=False))
    print()
    print(f"变更记录条数：{len(load_history())}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
