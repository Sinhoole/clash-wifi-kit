#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
给 Clash Verge 的全局扩展配置 Merge.yaml 自动加上 interface-name。

作用：让 Clash 的出站流量绑定到指定网卡（通常是无线网卡），
      从而实现"梯子只走 WiFi，其他软件走有线"。

用法：
    python add-interface-name.py                  # 自动探测网卡名并写入
    python add-interface-name.py --dry-run        # 只预览，不落盘
    python add-interface-name.py --name WLAN      # 手动指定网卡名
    python add-interface-name.py --restore        # 从最近一次备份还原

安全措施：
    * 写入前自动备份为 Merge.yaml.<时间戳>.bak
    * 幂等：已经有顶层 interface-name 就改值，不会重复添加
    * 写完重新读取校验
"""

import argparse
import datetime
import os
import re
import subprocess
import sys

KEY = "interface-name"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ------------------------------------------------------------------ 输出
def log(msg, tag="--"):
    print("  [ %s ] %s" % (tag, msg))


def head(title):
    print("")
    print("-" * 64)
    print("  " + title)
    print("-" * 64)


def ok(msg):
    log(msg, " OK ")


def warn(msg):
    log(msg, " !! ")


def bad(msg):
    log(msg, " XX ")


def info(msg):
    log(msg)


# ------------------------------------------------------------------ 定位配置目录
def candidate_dirs():
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        os.path.join(appdata, "io.github.clash-verge-rev.clash-verge-rev"),
        os.path.join(local, "io.github.clash-verge-rev.clash-verge-rev"),
        os.path.join(appdata, "clash-verge"),
        os.path.join(local, "clash-verge"),
    ]


def find_config_dir(override=None):
    if override:
        return override if os.path.isdir(override) else None
    for d in candidate_dirs():
        if d and os.path.isdir(os.path.join(d, "profiles")):
            return d
    # 退一步：只要目录存在就认
    for d in candidate_dirs():
        if d and os.path.isdir(d):
            return d
    return None


# ------------------------------------------------------------------ 网卡探测
def _run(args):
    try:
        p = subprocess.run(["netsh"] + args, capture_output=True)
    except Exception:
        return ""
    raw = p.stdout or b""
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


WL_RE = re.compile(r"WLAN|Wi-?Fi|Wireless|无线", re.I)


def list_adapters():
    out = []
    for line in _run(["interface", "ipv4", "show", "interfaces"]).splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$", line)
        if m:
            out.append({
                "idx": int(m.group(1)),
                "metric": int(m.group(2)),
                "state": m.group(4),
                "name": m.group(5),
            })
    return out


def detect_wifi_name():
    """优先用 netsh wlan 拿到无线网卡的真实名称，失败则按名字特征猜。"""
    for line in _run(["wlan", "show", "interfaces"]).splitlines():
        m = re.match(r"^\s*(名称|Name)\s*:\s*(\S.*?)\s*$", line)
        if m:
            return m.group(2), "netsh wlan show interfaces"
    names = [a["name"] for a in list_adapters() if WL_RE.search(a["name"])]
    if names:
        return names[0], "网卡名特征匹配"
    return "WLAN", "默认值（未探测到无线网卡）"


# ------------------------------------------------------------------ YAML 处理
def patch_text(text, value):
    lines = text.splitlines()
    top_idx = None
    indented = []

    for i, line in enumerate(lines):
        if re.match(r"^%s\s*:" % re.escape(KEY), line):
            top_idx = i
            break
        if re.match(r"^\s+%s\s*:" % re.escape(KEY), line):
            indented.append(i + 1)

    changed = False
    if top_idx is not None:
        old = lines[top_idx]
        new = "%s: %s" % (KEY, value)
        if old.strip() != new:
            lines[top_idx] = new
            changed = True
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append("# 下面这行让 Clash 的出站流量只从这块网卡出去")
        lines.append("%s: %s" % (KEY, value))
        changed = True

    return "\n".join(lines) + "\n", changed, indented


def make_backup(path):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = "%s.%s.bak" % (path, stamp)
    with open(path, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def newest_backup(path):
    d = os.path.dirname(path)
    base = os.path.basename(path) + "."
    cands = [os.path.join(d, n) for n in os.listdir(d)
             if n.startswith(base) and n.endswith(".bak")]
    return max(cands) if cands else None


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(
        description="给 Clash Verge 的 Merge.yaml 加上 interface-name（出站绑定网卡）")
    ap.add_argument("--name", help="网卡名称，默认自动探测")
    ap.add_argument("--dir", help="Clash Verge 配置目录，默认自动查找")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份")
    ap.add_argument("--restore", action="store_true", help="从最近一次备份还原")
    ap.add_argument("--force", action="store_true", help="网卡名不存在时也强制写入")
    args = ap.parse_args()

    head("1. 定位 Clash Verge 配置目录")
    cfg = find_config_dir(args.dir)
    if not cfg:
        bad("没找到 Clash Verge 配置目录。用 --dir 手动指定。")
        info("常见位置：%APPDATA%\\io.github.clash-verge-rev.clash-verge-rev")
        return 2
    ok(cfg)

    merge = os.path.join(cfg, "profiles", "Merge.yaml")
    if not os.path.isdir(os.path.dirname(merge)):
        bad("profiles 子目录不存在：" + os.path.dirname(merge))
        return 2

    head("2. 还原模式" if args.restore else "2. 确定网卡名")
    if args.restore:
        bak = newest_backup(merge)
        if not bak:
            bad("没有找到任何 .bak 备份")
            return 2
        if args.dry_run:
            warn("预览模式：本应还原自 " + bak)
            return 0
        with open(bak, "rb") as f:
            data = f.read()
        with open(merge, "wb") as f:
            f.write(data)
        ok("已从备份还原：" + os.path.basename(bak))
        info("回到 Clash Verge 点一下当前订阅让它生效")
        return 0

    adapters = list_adapters()
    if adapters:
        info("当前 IPv4 网卡：")
        for a in adapters:
            mark = "  <-- 无线" if WL_RE.search(a["name"]) else ""
            print("        idx=%-3d metric=%-5d %-12s %s%s"
                  % (a["idx"], a["metric"], a["state"], a["name"], mark))

    if args.name:
        name, src = args.name, "命令行指定"
    else:
        name, src = detect_wifi_name()
    ok("使用网卡名：%s  （来源：%s）" % (name, src))

    if adapters and not any(a["name"] == name for a in adapters):
        if args.force:
            warn("网卡列表里没有叫「%s」的网卡，但 --force 已指定，继续" % name)
        else:
            bad("网卡列表里没有叫「%s」的网卡 —— 名字写错 mihomo 会静默忽略！" % name)
            info("用 --name 指定上面列表里真实的名字，或加 --force 强行写入")
            return 3

    head("3. 读取 Merge.yaml")
    existed = os.path.isfile(merge)
    if existed:
        with open(merge, "rb") as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8-sig")
        except Exception:
            bad("Merge.yaml 不是 UTF-8 编码，拒绝修改（避免损坏你的配置）")
            return 4
        ok("已读取 %d 字节" % len(raw))
    else:
        text = ""
        warn("Merge.yaml 不存在，将新建")

    new_text, changed, indented = patch_text(text, name)

    if indented:
        bad("检测到第 %s 行有缩进的 %s —— 那是错的位置，必须顶格。"
            % (",".join(map(str, indented)), KEY))
        info("本脚本只处理顶格写法，请你手动删掉那些缩进行")

    if not changed:
        ok("已经是 interface-name: %s，无需修改" % name)
        print("")
        info("如果 Clash 里没生效，去 Clash Verge 点一下当前订阅重载。")
        return 0

    head("4. 修改预览")
    print("  ---- 修改后完整内容 ----")
    for line in new_text.splitlines():
        print("  | " + line)
    print("  ------------------------")

    if args.dry_run:
        warn("预览模式，未写入任何文件。去掉 --dry-run 才会真正保存。")
        return 0

    head("5. 备份与写入")
    try:
        if existed and not args.no_backup:
            bak = make_backup(merge)
            ok("已备份：" + os.path.basename(bak))

        tmp = merge + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_text)
        os.replace(tmp, merge)
    except PermissionError:
        bad("写入被拒绝。常见原因：")
        info("  1) Clash Verge 正打开着 Merge.yaml 的编辑窗口 —— 先关掉那个窗口")
        info("  2) 权限不足 —— 换个有写权限的账户，或检查目录属性")
        info("  3) 安全软件（火绒 / Defender 受控文件夹访问）拦了写入")
        info("原文件没有被改动，可安全重试。")
        return 6
    except OSError as e:
        bad("写入失败：%s" % e)
        info("原文件没有被改动，可安全重试。")
        return 6
    ok("已写入：" + merge)

    head("6. 自校验")
    with open(merge, "r", encoding="utf-8") as f:
        back = f.read()
    hit = [l for l in back.splitlines()
           if re.match(r"^%s\s*:" % re.escape(KEY), l)]
    if hit and hit[0].strip() == "%s: %s" % (KEY, name):
        ok("回读校验通过：" + hit[0].strip())
    else:
        bad("回读校验失败，请手动检查：" + merge)
        return 5

    head("7. 下一步（必须做，否则不生效）")
    info("1) 打开 Clash Verge →「订阅 / 配置」页")
    info("2) 点一下你正在用的那个订阅（或等它自动重载）")
    info("3) 验证运行配置里真的有了：")
    print('')
    print('     Select-String "%s" -Pattern "^interface-name"'
          % os.path.join(cfg, "clash-verge.yaml"))
    print('')
    info("4) 有输出才算生效。然后跑 clash-wifi.cmd 做 L2 / L3 检测。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
