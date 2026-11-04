# Clash WiFi Kit

让 Clash 的出站流量只走无线网卡，其余流量走有线网卡；无线断开时 Clash 立即失效，不回落到有线链路。

适用于同时连接两个网络（例如有线接办公网、无线接热点）且需要将代理流量隔离到指定链路的场景。

> Bind Clash egress to a specific NIC on Windows, with fail-closed behavior and full rollback support.

---

## 解决的问题

在 Windows 上同时接入两条链路时，默认路由由全局路由表按 metric 决定，无法按进程区分。这带来两个问题：

1. 代理流量可能落到不希望使用的链路上
2. 链路切换时行为不确定，且难以验证

本工具通过 `interface-name` 让 Clash 在每个出站 socket 上设置 `IP_UNICAST_IF`，将路由查找限定在指定网卡内，从而在不修改全局路由表的前提下实现按进程分流。

## 原理

```
普通程序   查全局路由表      → 有线（metric 小者胜出）
Clash      路由查找限于无线网卡 → 无线
无线断开   该网卡无任何路由   → connect() 返回 WSAENETUNREACH
                              → 不产生数据包，不存在回落可能
```

关键点：第三种情况由内核在发包前返回错误，因此是 fail-closed，而非静默回退。

## 功能

- 自动定位 Clash Verge 安装路径、内核与配置目录
- 检测网卡、路由、配置项，判断绑定是否真正生效
- 写入 `interface-name`、设置网卡优先级，操作前自动备份
- 基于连接源地址验证出口分布（无需联网）
- 断网测试：断开无线后复测，确认 fail-closed
- 看门狗：无线断开时关闭系统代理并退出 Clash
- 订阅加固：关闭订阅更新的直连路径，可生成本地副本彻底消除请求，避免整机断网
- 变更记录与回退：任何写操作均可撤销
- 图形界面与命令行两种形态

## 安装

### 方式一：安装包（推荐）

下载 `ClashWiFiKit-1.0-Setup.exe`，双击运行。安装到 `%LOCALAPPDATA%\Programs\ClashWiFiKit`，创建快捷方式与卸载条目，不需要管理员权限。

### 方式二：解压即用

下载 `ClashWiFiKit-1.0-Setup.zip`，解压后双击 `安装.cmd`。

### 方式三：从源码运行

需要 Python 3.9 及以上：

```cmd
python clash-wifi-gui.py
```

## 使用流程

界面按步骤组织，建议依序操作：

| 步骤 | 内容 | 成功标志 |
|---|---|---|
| 一 定位 | 自动查找 Clash 路径 | 界面、内核、配置目录三项均识别 |
| 二 检测 | 体检网卡、路由与配置 | 检查清单无失败项 |
| 三 配置 | 写入 `interface-name`，设置网卡优先级 | 运行配置中存在该字段 |
| 四 验证 | 出口分布、断网测试 | Clash 经无线出站；断网后请求全部失败 |
| 五 看门狗 | 处理无线断开后的副作用 | 无线断开时浏览器仍可访问国内站点 |
| 订阅加固 | 关闭订阅更新的直连路径 | 远程订阅显示「自动更新 否 / 走代理 是」 |

配置完成后需回到 Clash Verge 点击一次当前订阅，使其重新生成运行配置。

## 注意事项

`interface-name` 全局作用于 Clash 的全部出站，包含分流规则中标记为直连的流量。因此无线断开时，经 Clash 的请求会全部失败。若系统代理仍开启，浏览器将无法访问任何站点。看门狗用于处理该情形。

若 Clash Verge 启用了服务模式，内核以 SYSTEM 身份运行，普通权限无法终止。可在设置中卸载服务，或以管理员权限运行看门狗。

## 目录结构

```
clash-wifi-gui.py      图形界面（主程序）
clash-wifi.ps1         命令行检测工具
clash-watchdog.ps1     看门狗
Find-Clash.ps1         路径自动定位
add-interface-name.py  配置写入工具
installer.py           安装程序源码
build.ps1 / build.cmd  打包脚本
menu.ps1 / menu.cmd    命令行菜单
docs/                  流程图与界面截图
```

## 构建

```powershell
.\build.ps1            # 目录模式
.\build.ps1 -OneFile   # 单文件模式
```

生成 `dist\ClashWiFiKit\`。构建安装包需另行执行 `installer.py` 的打包，参见 `build.ps1` 中的参数。

## 免责声明

本工具仅修改本机网络配置与 Clash 配置文件。所有写操作在修改前生成备份并记入变更记录，可在程序内撤销。请在确认对设备具备配置权限的前提下使用。

## 许可证

MIT，详见 [LICENSE](LICENSE)。

## 相关

项目主页：<https://blog.taoxi.ink>
