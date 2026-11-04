#requires -Version 5.1
<#
.SYNOPSIS
    Clash 出口巡检 / 网卡优先级钉死 / 分流验证

.DESCRIPTION
    纯 netsh + route 实现，不依赖 WMI / CIM，受限环境也能跑。
    做三件事：
      inspect  体检：网卡分数、默认路由谁赢、Clash 有没有绑网卡、有没有隐性坑
      pin      把网卡分数钉死（有线优先），需要管理员 + -Apply 才真改
      verify   直连 vs 代理 两条 curl 对比，判断分流是否干净

.EXAMPLE
    .\clash-wifi.ps1
    .\clash-wifi.ps1 -Action inspect -WifiName WLAN -EthName "以太网 2"

.EXAMPLE
    # 真正修改分数（管理员 PowerShell）
    .\clash-wifi.ps1 -Action pin -Apply

.EXAMPLE
    .\clash-wifi.ps1 -Action verify -ProxyPort 7897
#>
[CmdletBinding()]
param(
    [ValidateSet('inspect', 'pin', 'verify', 'split', 'drill', 'help')]
    [string]$Action = 'inspect',

    [string]$WifiName  = 'WLAN',
    [string]$WifiProfile = '',
    [string]$EthName   = '以太网 2',

    [int]$EthMetric    = 10,
    [int]$WifiMetric   = 50,

    [string]$ProxyPort = '',

    [switch]$Apply
)

$ErrorActionPreference = 'Continue'

# ------------------------------------------------------------------ 输出helper
function Section([string]$t) {
    Write-Host ''
    Write-Host ('─' * 66) -ForegroundColor DarkGray
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ('─' * 66) -ForegroundColor DarkGray
}
function Ok([string]$m)   { Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Warn([string]$m) { Write-Host "  [ !! ] $m" -ForegroundColor Yellow }
function Bad([string]$m)  { Write-Host "  [ XX ] $m" -ForegroundColor Red }
function Info([string]$m) { Write-Host "  [ -- ] $m" -ForegroundColor Gray }

# ------------------------------------------------------------------ 数据采集
function Get-Adapters {
    $rows = @()
    $out = & netsh interface ipv4 show interfaces 2>$null
    if (-not $out) { return @() }
    foreach ($line in $out) {
        if ($line -match '^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(.+?)\s*$') {
            $rows += [pscustomobject]@{
                Index  = [int]$Matches[1]
                Metric = [int]$Matches[2]
                State  = $Matches[4]
                Name   = $Matches[5]
            }
        }
    }
    return $rows
}

function Get-DefaultRoutes {
    $rows = @()
    $out = & route print -4 2>$null
    if (-not $out) { return @() }
    foreach ($line in $out) {
        if ($line -match '^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s*$') {
            $rows += [pscustomobject]@{
                Gateway = $Matches[1]
                LocalIP = $Matches[2]
                Metric  = [int]$Matches[3]
            }
        }
    }
    return $rows | Sort-Object Metric -Unique
}

function Find-Adapter([string]$name) {
    $all = Get-Adapters
    $hit = $all | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $hit) { $hit = $all | Where-Object { $_.Name -like "*$name*" } | Select-Object -First 1 }
    return $hit
}

function Get-ClashDir {
    $cands = @(
        (Join-Path $env:APPDATA     'io.github.clash-verge-rev.clash-verge-rev'),
        (Join-Path $env:LOCALAPPDATA 'io.github.clash-verge-rev.clash-verge-rev'),
        (Join-Path $env:APPDATA     'clash-verge')
    )
    foreach ($c in $cands) {
        if (Test-Path (Join-Path $c 'profiles')) { return $c }
    }
    return $null
}

function Get-KvLine([string]$file, [string]$key, [int]$MaxLines = 5000) {
    if (-not (Test-Path $file)) { return $null }
    $pat = "^\s*$([regex]::Escape($key))\s*:\s*(.*)$"
    $hit = Get-Content $file -TotalCount $MaxLines -Encoding UTF8 -ErrorAction SilentlyContinue |
           Where-Object { $_ -match $pat } |
           Select-Object -First 1
    if (-not $hit) { return $null }
    $null  = $hit -match $pat
    $value = [string]$Matches[1]
    return [pscustomobject]@{
        Indented = [bool]($hit -match '^\s+\S')
        Value    = $value.Trim().Trim('"').Trim("'")
    }
}

function Resolve-ProxyPort {
    if ($ProxyPort) { return $ProxyPort }
    $dir = Get-ClashDir
    if ($dir) {
        foreach ($f in @('clash-verge.yaml', 'config.yaml')) {
            $p = Get-KvLine (Join-Path $dir $f) 'mixed-port'
            if ($p -and $p.Value) { return $p.Value }
        }
        foreach ($f in @('clash-verge.yaml', 'config.yaml')) {
            $p = Get-KvLine (Join-Path $dir $f) 'port'
            if ($p -and $p.Value) { return $p.Value }
        }
    }
    return '7897'
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ------------------------------------------------------------------ inspect
function Invoke-Inspect {
    $verdicts = New-Object System.Collections.ArrayList

    Section '1. 全部 IPv4 网卡'
    $ads = Get-Adapters
    if (-not $ads) { Bad '读不到网卡列表（netsh 不可用）'; return }
    $ads | Format-Table Index, Metric, State, Name -AutoSize | Out-String -Width 100 | Write-Host
    Info 'Metric 越小优先级越高。同一个子网/同一目的地的两条默认路由，只会有分数小的那条生效。'

    Section '2. 默认路由（谁能出网）'
    $routes = Get-DefaultRoutes
    if (-not $routes) {
        Bad '没有任何 IPv4 默认路由 —— 现在整台机器都出不了网'
        $null = $verdicts.Add('没有默认路由')
    }
    else {
        $routes | Format-Table Gateway, LocalIP, Metric -AutoSize | Out-String -Width 100 | Write-Host
        $win = $routes | Select-Object -First 1
        Ok "当前生效的出口：网关 $($win.Gateway) ／本机 $($win.LocalIP) ／分数 $($win.Metric)"
        if ($routes.Count -ge 2) {
            Info '两条以上默认路由 = 双网卡同时在线。只有分数最小的那条干活，不是负载均衡。'
        }
    }

    Section '3. 目标网卡分数对比'
    $wifi = Find-Adapter $WifiName
    $eth  = Find-Adapter $EthName
    if (-not $wifi) { Bad "找不到名为 '$WifiName' 的网卡。用上面第 1 节的真实名字重跑，或加 -WifiName" }
    if (-not $eth)  { Bad "找不到名为 '$EthName' 的网卡。用上面第 1 节的真实名字重跑，或加 -EthName" }

    if ($wifi -and $eth) {
        Info ("WiFi  [{0}] 分数 = {1}" -f $wifi.Name, $wifi.Metric)
        Info ("有线  [{0}] 分数 = {1}" -f $eth.Name,  $eth.Metric)
        if ($wifi.Metric -lt $eth.Metric) {
            Bad 'WiFi 分数更低 → 全系统所有软件（不只是 Clash）都会往 WiFi 跑！'
            $null = $verdicts.Add("WiFi metric($($wifi.Metric)) < 有线($($eth.Metric))，其他软件会被抢到 WiFi")
        }
        elseif ($wifi.Metric -eq $eth.Metric) {
            Bad '两边分数相同 → Windows 选谁不保证稳定，会表现为"网络时好时坏"'
            $null = $verdicts.Add('两条默认路由 metric 相同，出口不确定')
        }
        else {
            Ok '有线分数更低 → 其他软件稳定走宽带，符合预期'
        }
    }

    Section '4. Clash Verge 配置'
    $dir = Get-ClashDir
    if (-not $dir) {
        Warn '没找到 Clash Verge 配置目录，跳过（可能没装或用了别的客户端）'
    }
    else {
        Info "配置目录：$dir"
        $runtime = Join-Path $dir 'clash-verge.yaml'
        $merge   = Join-Path $dir 'profiles\Merge.yaml'
        $cfg     = Join-Path $dir 'config.yaml'
        $app     = Join-Path $dir 'verge.yaml'

        # 4.1 interface-name 是否真的进了运行配置
        $rt = Get-KvLine $runtime 'interface-name'
        if ($rt -and $rt.Value) {
            if ($rt.Indented) {
                Bad "clash-verge.yaml 里 interface-name 有缩进（值=$($rt.Value)）—— 位置错了，内核会忽略"
                $null = $verdicts.Add('interface-name 缩进错误')
            }
            else {
                Ok "运行配置已生效：interface-name = $($rt.Value)"
                if ($wifi -and $rt.Value -ne $wifi.Name) {
                    Warn "但和实际 WiFi 网卡名 '$($wifi.Name)' 不一致 —— 名字拼错时 mihomo 会静默忽略"
                    $null = $verdicts.Add('interface-name 值疑似拼错')
                }
            }
        }
        else {
            Bad '运行配置里没有 interface-name → Clash 没绑网卡，走默认路由（宽带）'
            Info '修法：Clash Verge →「订阅/配置」页 →「全局扩展配置」卡片，顶层加一行：'
            Write-Host '         interface-name: ' -NoNewline -ForegroundColor White
            Write-Host ($wifi.Name) -ForegroundColor Yellow
            Info '加完点一下当前订阅让它重新生成配置'
            $null = $verdicts.Add('Clash 未绑定网卡')
        }

        # 4.2 Merge.yaml 里写了没有
        if (Test-Path $merge) {
            $mg = Get-KvLine $merge 'interface-name'
            if ($mg) { Info "profiles\Merge.yaml 里已写：interface-name = $($mg.Value)" }
            else     { Info 'profiles\Merge.yaml 里还没写 interface-name' }
        }

        # 4.3 代理开关状态
        $sysProxy = Get-KvLine $app 'enable_system_proxy'
        $tunMode  = Get-KvLine $app 'enable_tun_mode'
        if ($sysProxy) {
            if ($sysProxy.Value -match 'true') { Ok '系统代理：已开启' }
            else {
                Warn '系统代理：关闭 → 目前没有流量进 Clash，绑了网卡也看不到效果'
                $null = $verdicts.Add('系统代理关闭')
            }
        }
        if ($tunMode) {
            if ($tunMode.Value -match 'true') {
                Warn 'TUN 模式：开启 → 所有流量被虚拟网卡接管，需确保代理服务器 IP 在 route-exclude-address 里，否则回环'
                $null = $verdicts.Add('TUN 开启，需检查 route-exclude-address')
            }
            else { Info 'TUN 模式：关闭' }
        }

        # 4.4 tun.stack 合法性
        $stack = Get-KvLine $cfg 'stack'
        if ($stack -and $stack.Value) {
            if ($stack.Value -in @('gvisor', 'system', 'mixed')) {
                Info "tun.stack = $($stack.Value)（合法）"
            }
            else {
                Bad "tun.stack = '$($stack.Value)' 不是合法值（只能是 gvisor / system / mixed）→ 开 TUN 会报错"
                $null = $verdicts.Add("tun.stack 非法值 '$($stack.Value)'")
            }
        }

        # 4.5 端口
        foreach ($k in @('mixed-port', 'socks-port', 'port')) {
            $p = Get-KvLine $cfg $k
            if ($p -and $p.Value) { Info ("{0,-12} = {1}   （楼内收件窗口，与路由无关）" -f $k, $p.Value) }
        }
    }

    Section '5. 结论'
    if ($verdicts.Count -eq 0) {
        Ok '没发现问题。再用 -Action verify 做实际流量验证。'
    }
    else {
        Warn ("发现 {0} 个需要注意的点：" -f $verdicts.Count)
        $i = 0
        foreach ($v in $verdicts) { $i++; Write-Host ("         {0}. {1}" -f $i, $v) -ForegroundColor Yellow }
    }
    Write-Host ''
}

# ------------------------------------------------------------------ pin
function Invoke-Pin {
    Section '网卡优先级钉死'

    $wifi = Find-Adapter $WifiName
    $eth  = Find-Adapter $EthName
    if (-not $eth)  { Bad "找不到网卡 '$EthName'，用 -EthName 指定真实名字"; return }
    if (-not $wifi) { Bad "找不到网卡 '$WifiName'，用 -WifiName 指定真实名字"; return }

    Info ("将有线 [{0}] 分数设为 {1}，WiFi [{2}] 分数设为 {3}" -f $eth.Name, $EthMetric, $wifi.Name, $WifiMetric)
    Info '目的：其他所有软件稳定走宽带；Clash 靠 interface-name 单独走 WiFi'

    if (-not $Apply) {
        Write-Host ''
        Warn '这是预演模式，什么都没改。确认无误后加 -Apply 真正执行：'
        Write-Host "         .\clash-wifi.ps1 -Action pin -Apply" -ForegroundColor White
        Write-Host ''
        return
    }

    if (-not (Test-IsAdmin)) {
        Bad '需要管理员权限。右键 PowerShell「以管理员身份运行」后重试。'
        return
    }

    $r1 = & netsh interface ipv4 set interface "$($eth.Name)" metric=$EthMetric 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "有线 [$($eth.Name)] → $EthMetric" } else { Bad "设置有失败：$r1" }

    $r2 = & netsh interface ipv4 set interface "$($wifi.Name)" metric=$WifiMetric 2>&1
    if ($LASTEXITCODE -eq 0) { Ok "WiFi [$($wifi.Name)] → $WifiMetric" } else { Bad "设置有失败：$r2" }

    Section '改后确认'
    $ads = Get-Adapters
    $ads | Format-Table Index, Metric, State, Name -AutoSize | Out-String -Width 100 | Write-Host
    $w = Find-Adapter $WifiName
    $e = Find-Adapter $EthName
    if ($w -and $e) {
        if ($e.Metric -lt $w.Metric) { Ok '有线分数更低，其他软件会走宽带。配置正确。' }
        else { Bad '有线分数仍然不低，检查是否有组策略/DHCP 强制下发了分数' }
    }
    Write-Host ''
}

# ------------------------------------------------------------------ verify
function Invoke-Curl([string]$url, [string]$proxy) {
    $a = @('-s', '--max-time', '12')
    if ($proxy) { $a += @('-x', $proxy) }
    $a += $url
    $out = & curl.exe @a 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    return (($out | Out-String).Trim())
}

function Invoke-Verify {
    Section '实际流量验证'

    if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
        Bad '找不到 curl.exe（Win10 1803 以下没有）。换台机器或用浏览器手动测。'
        return
    }

    $port = Resolve-ProxyPort
    $proxyUrl = "http://127.0.0.1:$port"
    Info "代理端口：$proxyUrl（自动从运行配置读取）"

    $direct = Invoke-Curl 'https://api.ipify.org' $null
    $viaProxy = Invoke-Curl 'https://api.ipify.org' $proxyUrl

    Write-Host ''
    if ($direct)  { Ok "直连出口 IP ：$direct" }  else { Bad '直连失败（可能本来就没网）' }
    if ($viaProxy) { Ok "代理出口 IP ：$viaProxy" } else { Bad "代理失败（Clash 没启动 / 端口不是 $port / 系统代理没开）" }

    Write-Host ''
    if ($direct -and $viaProxy) {
        if ($direct -ne $viaProxy) {
            Ok '两个 IP 不同 → 分流干净：直连走宽带，梯子走 WiFi'
        }
        else {
            Bad '两个 IP 相同 → 有地方串了，按下面判读：'
            Info '  都是机场节点 IP → 系统代理/TUN 劫持了所有流量（分流规则的事，与网卡绑定无关）'
            Info '  都是宽带 IP     → Clash 没绑网卡，梯子其实走宽带'
            Info '  都是 WiFi IP    → 网卡 metric 排序错了，跑 -Action pin'
        }
    }

    Section '决定性测试（手动做，最硬）'
    Info '管理员 PowerShell 依次执行，看夹在中间那条还通不通：'
    Write-Host ''
    Write-Host '  netsh interface set interface "' -NoNewline -ForegroundColor White
    Write-Host $EthName -NoNewline -ForegroundColor Yellow
    Write-Host '" admin=disable' -ForegroundColor White
    Write-Host "  curl -x $proxyUrl https://api.ipify.org" -ForegroundColor White
    Write-Host ''
    Info '  还通  → 梯子确实走 WiFi，成功'
    Info '  不通  → 梯子其实走宽带，绑定没生效'
    Write-Host ''
    Write-Host '  netsh interface set interface "' -NoNewline -ForegroundColor White
    Write-Host $EthName -NoNewline -ForegroundColor Yellow
    Write-Host '" admin=enable    # 记得恢复' -ForegroundColor White
    Write-Host ''
}

# ------------------------------------------------------------------ drill
function Test-WifiUp {
    $w = Find-Adapter $WifiName
    if (-not $w) { return $false }
    return ($w.State -match 'connected|已连接')
}

function Invoke-Drill {
    Section 'fail-closed 断网实测'

    Info '原理：Clash 绑了 WiFi 网卡，WiFi 一断，18 号网卡上没有任何路由 → connect() 直接失败'
    Info '      若此时代理仍然返回 IP，说明它已经漏到校园网了 —— 这就是 fail-open'
    Write-Host ''

    if (-not $Apply) {
        Warn '这一步会真的断开你的 WiFi，所以必须显式确认。'
        Write-Host ''
        Info '确认后在管理员 PowerShell 里执行：'
        Write-Host '         .\clash-wifi.cmd -Action drill -Apply' -ForegroundColor White
        Write-Host ''
        Info "（当前 WiFi 配置文件/SSID 按「$WifiProfile」处理，不对就加 -WifiProfile 参数）"
        return
    }

    if (-not (Test-IsAdmin)) { Bad '需要管理员权限（netsh wlan 需要提权）'; return }
    if (-not (Test-WifiUp))  { Bad "WiFi [$WifiName] 当前不是已连接状态，无法做这个测试"; return }

    $port = Resolve-ProxyPort
    $proxyUrl = "http://127.0.0.1:$port"
    Info "代理端口：$proxyUrl"

    # --- 阶段 1：断开前，记录基线
    Section '阶段 1 / 3  断开前的基线'
    $before = Invoke-Curl 'https://api.ipify.org' $proxyUrl
    if (-not $before) {
        Bad "代理现在就不通（$proxyUrl）。先让梯子跑起来再做这个测试。"
        return
    }
    Ok "代理出口 IP ：$before"

    # --- 阶段 2：断开 WiFi，立刻复测
    Section '阶段 2 / 3  断开 WiFi 后复测'
    Info "执行 netsh wlan disconnect ..."
    & netsh wlan disconnect interface="$WifiName" 2>&1 | Out-Null
    Start-Sleep -Seconds 4

    if (Test-WifiUp) { Warn 'WiFi 似乎还没断开，再等 4 秒' ; Start-Sleep -Seconds 4 }

    $during = Invoke-Curl 'https://api.ipify.org' $proxyUrl

    # --- 阶段 3：恢复 WiFi
    Section '阶段 3 / 3  恢复 WiFi'
    $restored = $false
    if (-not $WifiProfile) {
        Warn '未指定 -WifiProfile，无法自动重连，请手动连接无线网络'
    }
    for ($i = 1; $i -le 5 -and $WifiProfile; $i++) {
        if (-not (Test-WifiUp)) {
            & netsh wlan connect name="$WifiProfile" ssid="$WifiProfile" interface="$WifiName" 2>&1 | Out-Null
        }
        Start-Sleep -Seconds 3
        if (Test-WifiUp) { $restored = $true; break }
        Info "第 $i 次尝试重连未成功..."
    }

    if ($restored) {
        Ok "WiFi 已恢复（$WifiProfile）"
        Start-Sleep -Seconds 3
        $after = Invoke-Curl 'https://api.ipify.org' $proxyUrl
        if ($after) { Ok "重建后代理出口 IP ：$after" } else { Warn 'WiFi 回来了但代理还没通，稍等重试或重启 Clash' }
    }
    else {
        Bad '自动重连失败。手动点一下任务栏的 WiFi 图标连回去。'
        Info "也可手动执行： netsh wlan connect name=「$WifiProfile」"
    }

    # --- 结论
    Section '结论'
    if ($null -eq $during) {
        Ok '断开 WiFi 后代理失败 → 【fail-closed 达成】正是你要的行为'
        Info '梯子不会回落到校园网，WiFi 一断就是直接死'
    }
    elseif ($during -eq $before) {
        Bad '断开 WiFi 后代理仍然返回同一个 IP → 说明流量没走 WiFi，或者压根没断成功'
        Info '跑 -Action inspect，重点看第 4 节「运行配置里有没有 interface-name」'
    }
    else {
        Bad "断开 WiFi 后代理返回了不同的 IP：$during"
        Info '这就是 fail-open：梯子已经漏到别的链路（大概是校园网）去了'
        Info '需要在 Clash 之外再加一层防火墙：禁止 mihomo 内核从以太网出站'
    }
    Write-Host ''
}

# ------------------------------------------------------------------ split
function Get-AdapterIPv4([string]$name) {
    foreach ($l in (& netsh interface ipv4 show config name="$name" 2>$null)) {
        if ($l -match 'IP Address:\s*(\d+\.\d+\.\d+\.\d+)') { return $Matches[1] }
    }
    return $null
}

function Get-ListenerPid([string]$port) {
    foreach ($l in (netstat -ano | Select-String -Pattern ":$port\s")) {
        $f = ($l.Line.Trim() -split '\s+')
        if ($f.Count -ge 5 -and $f[3] -eq 'LISTENING') { return [int]$f[4] }
    }
    return $null
}

function Get-Established {
    $rows = @()
    foreach ($l in (netstat -ano | Select-String -Pattern '^\s*TCP\s')) {
        $f = ($l.Line.Trim() -split '\s+')
        if ($f.Count -ge 5 -and $f[3] -eq 'ESTABLISHED') {
            $src = ($f[1] -split ':')[0]
            if ($src -match '^\d+\.\d+\.\d+\.\d+$') {
                $rows += [pscustomobject]@{ Src = $src; Remote = $f[2]; Pid = [int]$f[4] }
            }
        }
    }
    return $rows
}

function Get-PidName($p) {
    try { return (Get-Process -Id $p -ErrorAction Stop).ProcessName } catch { return "pid:$p" }
}

function Invoke-Split {
    Section '出口分布检查：谁走 WiFi，谁走有线'

    $port  = Resolve-ProxyPort
    $wip   = Get-AdapterIPv4 $WifiName
    $eip   = Get-AdapterIPv4 $EthName
    $cpid  = Get-ListenerPid $port

    Info "WiFi [$WifiName] IPv4 = $wip"
    Info "有线 [$EthName]  IPv4 = $eip"
    if ($cpid) { Ok "Clash 内核 PID = $cpid（监听 $port）" } else { Bad "没找到监听 $port 的进程，Clash 没在跑？" }

    if (-not $wip) { Bad "WiFi 没有 IPv4 地址，无法判断"; return }
    if (-not $eip) { Bad "有线没有 IPv4 地址 —— 只有一条路时无法证明绑定生效"; return }

    $conns = Get-Established
    if (-not $conns) { Warn '当前没有任何已建立的 TCP 连接，先去浏览几个网页再测'; return }

    # 按源 IP 分组
    Section '按出口网卡分组'
    foreach ($g in ($conns | Group-Object Src | Sort-Object Count -Descending)) {
        $tag = ''
        if ($g.Name -eq $wip) { $tag = '  <- WiFi' }
        elseif ($g.Name -eq $eip) { $tag = '  <- 有线' }
        "  {0,-18} {1,3} 条{2}" -f $g.Name, $g.Count, $tag
        foreach ($pg in ($g.Group | Group-Object Pid)) {
            $nm = Get-PidName $pg.Name
            $mark = if ($pg.Name -eq $cpid) { '  [Clash 内核]' } else { '' }
            "        {0,-22} {1,2} 条{2}" -f $nm, $pg.Count, $mark
        }
    }

    # 判定
    Section '结论'
    $clashOnWifi = @($conns | Where-Object { $_.Pid -eq $cpid -and $_.Src -eq $wip }).Count
    $clashOnEth  = @($conns | Where-Object { $_.Pid -eq $cpid -and $_.Src -eq $eip }).Count
    $otherOnWifi = @($conns | Where-Object { $_.Pid -ne $cpid -and $_.Src -eq $wip }).Count
    $otherOnEth  = @($conns | Where-Object { $_.Pid -ne $cpid -and $_.Src -eq $eip }).Count

    Info "Clash  → WiFi: $clashOnWifi 条 ｜ 有线: $clashOnEth 条"
    Info "其他程序 → WiFi: $otherOnWifi 条 ｜ 有线: $otherOnEth 条"
    Write-Host ''

    $pass = $true
    if ($clashOnWifi -gt 0) { Ok 'Clash 正在走 WiFi —— interface-name 生效' }
    else { Bad 'Clash 没有一条走 WiFi —— 绑定没生效，或此刻没有活动的翻墙连接'; $pass = $false }

    if ($clashOnEth -gt 0) { Bad "Clash 有 $clashOnEth 条走有线 —— 在漏，需要防火墙兜底"; $pass = $false }
    else { Ok 'Clash 完全没有走有线 —— 没有泄漏' }

    if ($otherOnWifi -gt 0) { Warn "其他程序有 $otherOnWifi 条走 WiFi —— 可能是它们在主动选网卡，或 WiFi 分数太低" }
    else { Ok '其他程序全部走有线 —— 隔离达成' }

    if ($otherOnEth -eq 0 -and $otherOnWifi -gt 0) { Warn '注意：有线上一片空白，可能其他程序都被 WiFi 抢走了' }

    Write-Host ''
    if ($pass) { Ok '综合判定：方案按预期工作' } else { Warn '综合判定：未达标，先跑 -Action inspect' }
    Write-Host ''
}

# ------------------------------------------------------------------ main
switch ($Action) {
    'inspect' { Invoke-Inspect }
    'pin'     { Invoke-Pin }
    'verify'  { Invoke-Verify }
    'split'   { Invoke-Split }
    'drill'   { Invoke-Drill }
    'help'    {
        Get-Help $PSCommandPath -Detailed
    }
}
