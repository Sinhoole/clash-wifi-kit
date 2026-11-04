#requires -Version 5.1
<#
.SYNOPSIS
    WLAN 掉线看门狗：WiFi 一断，持续保证「系统代理关闭 + Clash 退出」。

.DESCRIPTION
    解决的问题：
      interface-name 把 Clash 的出站焊死在无线网卡上。WiFi 一断，Clash 连不上任何
      东西；而系统代理还开着时，浏览器会把所有流量（含百度、Bing 这类国内站）都交给
      Clash，结果全部失败 —— 整台机器上不了网。

    关键点：Clash Verge 启动时会用它记住的状态重新打开系统代理。
      所以「只在状态变化时动作一次」不够，必须持续纠正。
      每一轮轮询都会检查：只要 WLAN 是断的，就确保
        (1) 系统代理 = 关闭
        (2) Clash 进程 = 不在
      被 Clash Verge 又打开了，下一轮会再关掉。

.EXAMPLE
    .\clash-watchdog.ps1 -Once -DryRun
    .\clash-watchdog.ps1
    .\clash-watchdog.ps1 -NoEnforce
    .\clash-watchdog.ps1 -KeepClash
    .\clash-watchdog.ps1 -RestoreOnUp -ClashExe "C:\...\Clash Verge.exe"
#>
[CmdletBinding()]
param(
    [string]$WifiName = 'WLAN',
    [int]$IntervalSeconds = 3,
    [switch]$KeepClash,
    [switch]$NoEnforce,
    [switch]$RestoreOnUp,
    [string]$ClashExe = '',
    [string]$LogFile = '',
    [switch]$Once,
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'
if (-not $LogFile) { $LogFile = Join-Path (Split-Path -Parent $PSCommandPath) 'clash-watchdog.log' }
$ProxyKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'

function Write-Log([string]$msg, [string]$tag = 'INFO') {
    $line = "{0} [{1,-5}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $tag, $msg
    $color = switch ($tag) { 'DOWN' { 'Yellow' } 'UP' { 'Green' } 'ERROR' { 'Red' } 'DRY' { 'Magenta' } default { 'Gray' } }
    Write-Host $line -ForegroundColor $color
    try {
        Add-Content -Path $LogFile -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd '), $line) -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

function Get-WlanState {
    $out = & netsh interface ipv4 show interfaces 2>$null
    if (-not $out) { return 'unknown' }
    foreach ($line in $out) {
        if ($line -match '^\s*\d+\s+\d+\s+\d+\s+(\S+)\s+(.+?)\s*$') {
            $state = $Matches[1]
            $name = $Matches[2]
            if ($name -eq $WifiName) {
                if ($state -match 'disconnected|已断开') { return 'down' }
                if ($state -match '^connected|已连接') { return 'up' }
                return 'down'
            }
        }
    }
    return 'absent'
}

function Get-ProxyEnable {
    try { return [int](Get-ItemProperty $ProxyKey -ErrorAction Stop).ProxyEnable } catch { return -1 }
}

function Revive-WinInet {
    try {
        if (-not ('Wdg32' -as [type])) {
            Add-Type -Namespace '' -Name Wdg32 -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("wininet.dll", SetLastError = true)]
public static extern bool InternetSetOption(IntPtr h, int o, IntPtr b, int l);
'@
        }
        [void][Wdg32]::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)
        [void][Wdg32]::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)
    } catch { Write-Log ("刷新 WinINET 失败（浏览器可能要手动重启）: {0}" -f $_.Exception.Message) 'ERROR' }
}

function Set-SystemProxy([bool]$enable) {
    $want = if ($enable) { 1 } else { 0 }
    if ((Get-ProxyEnable) -eq $want) { return $true }
    if ($DryRun) { Write-Log ("本应把系统代理设为{0}" -f $(if ($enable) { '开启' } else { '关闭' })) 'DRY'; return $true }
    try {
        Set-ItemProperty $ProxyKey -Name ProxyEnable -Value $want -ErrorAction Stop
        Revive-WinInet
        $back = Get-ProxyEnable
        if ($back -ne $want) { throw ("回读不一致，仍为 {0}" -f $back) }
        Write-Log ("系统代理已{0}" -f $(if ($enable) { '开启' } else { '关闭' }))
        return $true
    } catch {
        Write-Log ("设置系统代理失败: {0}" -f $_.Exception.Message) 'ERROR'
        return $false
    }
}

function Get-ClashProcs {
    $rows = @()
    foreach ($l in (& tasklist 2>$null)) {
        if ($l -match '^\s*(\S+\.exe)\s+(\d+)\s') {
            $n = $Matches[1]
            $p = [int]$Matches[2]
            if ($n -match 'mihomo|clash') { $rows += [pscustomobject]@{ Name = $n; Pid = $p } }
        }
    }
    return $rows
}

function Stop-Clash {
    $ps = Get-ClashProcs
    if (-not $ps) { return $true }

    if ($DryRun) {
        Write-Log ("本应退出 {0} 个进程：{1}" -f $ps.Count, (($ps | ForEach-Object { "$($_.Name)($($_.Pid))" }) -join ', ')) 'DRY'
        return $true
    }

    $ordered = @($ps | Where-Object { $_.Name -notmatch 'mihomo' }) + @($ps | Where-Object { $_.Name -match 'mihomo' })
    foreach ($p in $ordered) {
        if ($p.Name -match 'service') { continue }
        [void](& taskkill /F /PID $p.Pid 2>&1)
        if ($LASTEXITCODE -eq 0) { Write-Log ("已退出 {0} (PID {1})" -f $p.Name, $p.Pid) }
    }

    Start-Sleep -Milliseconds 700
    $left = Get-ClashProcs
    if ($left.Count -eq 0) { return $true }

    Write-Log ("仍有 {0} 个未能退出：{1}" -f $left.Count, (($left | ForEach-Object { "$($_.Name)($($_.Pid))" }) -join ', ')) 'ERROR'
    Write-Log '通常是「服务模式」：内核由 clash_verge_service 以 SYSTEM 托管，普通权限杀不掉。' 'ERROR'
    Write-Log '解决：① 管理员运行本脚本；② Clash Verge 里点「虚拟网卡模式」右侧垃圾桶卸载服务；③ 管理员 net stop clash_verge_service' 'ERROR'
    return $false
}

function Start-Clash {
    if (-not $RestoreOnUp) { return }
    if (-not $ClashExe) {
        $fc = Join-Path (Split-Path -Parent $PSCommandPath) 'Find-Clash.ps1'
        if (Test-Path $fc) {
            try {
                $j = & $fc -Json 2>$null | ConvertFrom-Json
                if ($j -and $j.GuiExe) {
                    $script:ClashExe = $j.GuiExe
                    Write-Log ('自动定位到 Clash: ' + $j.GuiExe)
                }
            } catch { }
        }
    }
    if (-not $ClashExe) { Write-Log '未指定 -ClashExe 且自动定位失败，请手动启动 Clash Verge' 'ERROR'; return }
    if (-not (Test-Path $ClashExe)) { Write-Log ("找不到 Clash: {0}" -f $ClashExe) 'ERROR'; return }
    if ($DryRun) { Write-Log ("本应启动 {0}" -f $ClashExe) 'DRY'; return }
    try { Start-Process -FilePath $ClashExe | Out-Null; Write-Log ("已启动 {0}" -f $ClashExe) }
    catch { Write-Log ("启动失败: {0}" -f $_.Exception.Message) 'ERROR' }
}

function Test-NeedsFix {
    if ((Get-ProxyEnable) -eq 1) { return $true }
    if (-not $KeepClash -and (Get-ClashProcs).Count -gt 0) { return $true }
    return $false
}

function Invoke-Converge([string]$why) {
    Write-Log ("WLAN 断开 -> 收敛: {0}" -f $why) 'DOWN'
    [void](Set-SystemProxy $false)
    if (-not $KeepClash) { [void](Stop-Clash) }
    else { Write-Log '按 -KeepClash，保留 Clash 进程' }
}

function Invoke-OnUp {
    Write-Log 'WLAN 恢复' 'UP'
    if ($RestoreOnUp) { [void](Set-SystemProxy $true); Start-Clash }
    else { Write-Log '未指定 -RestoreOnUp，系统代理与 Clash 需你手动恢复' }
}

Write-Log ("看门狗启动 网卡={0} 轮询={1}s 杀Clash={2} 持续纠正={3} 恢复还原={4} 预演={5}" -f `
    $WifiName, $IntervalSeconds, (-not $KeepClash), (-not $NoEnforce), $RestoreOnUp, $DryRun)

$last = Get-WlanState
Write-Log ("初始状态: {0}" -f $last)

if ($last -eq 'absent') {
    Write-Log ("找不到网卡「{0}」，退出。用 -WifiName 指定真实名字。" -f $WifiName) 'ERROR'
    exit 2
}

if ($Once) {
    if ($last -eq 'up') { Write-Log 'WLAN 正常，无需处理' }
    else { Invoke-Converge '启动时 WLAN 即处于断开状态' }
    exit 0
}

if ($last -ne 'up') { Invoke-Converge '启动时 WLAN 即处于断开状态' }

$fixCount = 0
while ($true) {
    Start-Sleep -Seconds $IntervalSeconds
    $now = Get-WlanState
    $isDown = ($now -eq 'down' -or $now -eq 'absent')

    if ($isDown) {
        if ($NoEnforce) {
            if ($last -eq 'up' -or $last -eq 'unknown') { Invoke-Converge '状态切换为断开' }
        }
        elseif (Test-NeedsFix) {
            $fixCount++
            if ($fixCount -le 3 -or ($fixCount % 20) -eq 0) {
                Invoke-Converge ("第 {0} 次纠正（可能被 Clash Verge 重新打开）" -f $fixCount)
            }
            else {
                [void](Set-SystemProxy $false)
                if (-not $KeepClash) { [void](Stop-Clash) }
            }
        }
    }
    else {
        if ($last -ne 'up') { Invoke-OnUp }
        if ($fixCount -gt 0) { $fixCount = 0 }
    }
    $last = $now
}
