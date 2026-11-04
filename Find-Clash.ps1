#requires -Version 5.1
<#
.SYNOPSIS
    自动定位 Clash Verge 的安装路径、内核路径和配置目录。

.DESCRIPTION
    像 Geek Uninstaller 那样，用多条线索交叉查找，不依赖用户手填：

      1) 正在运行的进程
      2) 注册表卸载项（HKLM / HKCU / WOW6432Node 的 Uninstall）
      3) App Paths 注册项
      4) 桌面 / 开始菜单的快捷方式（.lnk 解引用）
      5) 常见安装目录
      6) 配置目录：扫描 %APPDATA% / %LOCALAPPDATA% 下含 profiles\Merge.yaml 的目录

    最后一招：-Pick 会列出桌面上的相关快捷方式让你点选，等价于"双击图标找路径"。

.EXAMPLE
    .\Find-Clash.ps1
    .\Find-Clash.ps1 -Pick
    .\Find-Clash.ps1 -Json
#>
[CmdletBinding()]
param(
    [switch]$Pick,
    [switch]$Json,
    [string]$ConfigDir = ''
)

$ErrorActionPreference = 'SilentlyContinue'
$found = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList

function Add-Hit([string]$kind, [string]$path, [string]$src) {
    if (-not $path) { return }
    if (-not (Test-Path $path)) { return }
    foreach ($h in $found) {
        if ($h.Path -ieq $path) { return }
    }
    [void]$found.Add([pscustomobject]@{ Kind = $kind; Path = $path; Source = $src })
}

# ---------------------------------------------------------------- 1) 运行中的进程
function From-Process {
    foreach ($l in (& tasklist 2>$null)) {
        if ($l -match '^\s*(\S+\.exe)\s+(\d+)\s') {
            $name = $Matches[1]
            if ($name -notmatch 'mihomo|clash') { continue }
            $img = & tasklist /FI ("PID eq " + $Matches[2]) /FO LIST 2>$null |
                   Select-String -Pattern '映像名称|Image Name'
            # 用 wmic 拿完整路径（失败就跳过）
            $p = & wmic process where ("processid=" + $Matches[2]) get ExecutablePath /value 2>$null |
                 Select-String -Pattern '='
            if ($p) {
                $v = ($p.Line -split '=', 2)[1].Trim()
                if ($v) {
                    $kind = if ($name -match 'mihomo') { 'core' } else { 'gui' }
                    Add-Hit $kind $v '正在运行的进程'
                }
            }
        }
    }
}

# ---------------------------------------------------------------- 2) 注册表卸载项
function From-Uninstall {
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($r in $roots) {
        foreach ($k in (Get-ChildItem $r -ErrorAction SilentlyContinue)) {
            $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
            if (-not $p) { continue }
            $dn = "$($p.DisplayName)"
            if ($dn -notmatch 'clash|verge|mihomo') { continue }
            $loc = "$($p.InstallLocation)"
            $ico = "$($p.DisplayIcon)"
            if ($loc -and (Test-Path $loc)) {
                foreach ($exe in (Get-ChildItem $loc -Filter '*.exe' -ErrorAction SilentlyContinue)) {
                    $kind = if ($exe.Name -match 'mihomo') { 'core' } else { 'gui' }
                    Add-Hit $kind $exe.FullName '注册表卸载项'
                }
            }
            if ($ico) {
                $ico = ($ico -replace ',\d+$', '').Trim('"')
                if (Test-Path $ico) { Add-Hit 'gui' $ico '注册表卸载项(DisplayIcon)' }
            }
        }
    }
}

# ---------------------------------------------------------------- 3) App Paths
function From-AppPaths {
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths'
    )
    foreach ($r in $roots) {
        foreach ($k in (Get-ChildItem $r -ErrorAction SilentlyContinue)) {
            if ($k.PSChildName -notmatch 'clash|verge|mihomo') { continue }
            $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
            if ($p.'(default)') { Add-Hit 'gui' $p.'(default)' 'App Paths 注册项' }
        }
    }
}

# ---------------------------------------------------------------- 4) 快捷方式
function Resolve-Lnk([string]$lnk) {
    try {
        $sh = New-Object -ComObject WScript.Shell
        $s = $sh.CreateShortcut($lnk)
        return $s.TargetPath
    } catch { return $null }
}

function From-Shortcuts {
    $dirs = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('CommonDesktopDirectory'),
        [Environment]::GetFolderPath('Programs'),
        [Environment]::GetFolderPath('CommonPrograms')
    )
    foreach ($d in $dirs) {
        if (-not $d -or -not (Test-Path $d)) { continue }
        foreach ($f in (Get-ChildItem $d -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue)) {
            if ($f.Name -notmatch 'clash|verge') { continue }
            $t = Resolve-Lnk $f.FullName
            if ($t -and (Test-Path $t)) {
                $kind = if ($t -match 'mihomo') { 'core' } else { 'gui' }
                Add-Hit $kind $t ("快捷方式: " + $f.Name)
            }
        }
    }
}

# ---------------------------------------------------------------- 5) 常见目录
function From-CommonDirs {
    $dirs = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\clash-verge'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Clash Verge'),
        (Join-Path $env:LOCALAPPDATA 'Clash Verge'),
        (Join-Path $env:ProgramFiles 'Clash Verge'),
        (Join-Path ${env:ProgramFiles(x86)} 'Clash Verge'),
        'C:\Clash Verge'
    )
    foreach ($d in $dirs) {
        if (-not $d -or -not (Test-Path $d)) { continue }
        foreach ($exe in (Get-ChildItem $d -Filter '*.exe' -ErrorAction SilentlyContinue)) {
            $kind = if ($exe.Name -match 'mihomo') { 'core' } else { 'gui' }
            Add-Hit $kind $exe.FullName '常见安装目录'
        }
    }
}

# ---------------------------------------------------------------- 6) 配置目录
function Find-ConfigDir {
    if ($ConfigDir -and (Test-Path (Join-Path $ConfigDir 'profiles'))) { return $ConfigDir }
    foreach ($root in @($env:APPDATA, $env:LOCALAPPDATA)) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        foreach ($d in (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue)) {
            $prof = Join-Path $d.FullName 'profiles'
            if ((Test-Path (Join-Path $prof 'Merge.yaml')) -or (Test-Path (Join-Path $d.FullName 'clash-verge.yaml'))) {
                return $d.FullName
            }
        }
    }
    return $null
}

# ---------------------------------------------------------------- 手动选图标
function Pick-FromDesktop {
    $d = [Environment]::GetFolderPath('Desktop')
    $lnks = @(Get-ChildItem $d -Filter '*.lnk' -ErrorAction SilentlyContinue)
    if (-not $lnks) { return }
    Write-Host ''
    Write-Host '  桌面上的快捷方式：' -ForegroundColor Cyan
    $i = 0
    foreach ($f in $lnks) {
        $i++
        $t = Resolve-Lnk $f.FullName
        Write-Host ("    [{0}] {1}" -f $i, $f.BaseName) -NoNewline
        if ($t) { Write-Host ("   -> " + $t) -ForegroundColor DarkGray } else { Write-Host '   (无法解析)' -ForegroundColor DarkGray }
    }
    Write-Host ''
    $sel = Read-Host '  输入编号选中 Clash 的图标（直接回车跳过）'
    if ($sel -match '^\d+$') {
        $idx = [int]$sel - 1
        if ($idx -ge 0 -and $idx -lt $lnks.Count) {
            $t = Resolve-Lnk $lnks[$idx].FullName
            if ($t -and (Test-Path $t)) {
                Add-Hit 'gui' $t '用户手动选择'
                $sib = Get-ChildItem (Split-Path $t -Parent) -Filter '*mihomo*.exe' -ErrorAction SilentlyContinue
                foreach ($s in $sib) { Add-Hit 'core' $s.FullName '用户手动选择(同目录)' }
            }
        }
    }
}

# ---------------------------------------------------------------- 执行
From-Process
From-Uninstall
From-AppPaths
From-Shortcuts
From-CommonDirs

if ($Pick) {
    $hasGui = @($found | Where-Object { $_.Kind -eq 'gui' }).Count -gt 0
    if (-not $hasGui) { Pick-FromDesktop }
    else {
        Write-Host ''
        Write-Host '  已自动定位到，无需手动选择' -ForegroundColor DarkGray
    }
}

$cfg = Find-ConfigDir

$gui  = @($found | Where-Object { $_.Kind -eq 'gui' })  | Select-Object -First 1
$core = @($found | Where-Object { $_.Kind -eq 'core' }) | Select-Object -First 1

# 若只有 GUI，尝试在同目录找内核
if (-not $core -and $gui) {
    $sib = Get-ChildItem (Split-Path $gui.Path -Parent) -Filter '*mihomo*.exe' -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($sib) {
        [void]$found.Add([pscustomobject]@{ Kind = 'core'; Path = $sib.FullName; Source = '同目录推断' })
        $core = [pscustomobject]@{ Kind = 'core'; Path = $sib.FullName; Source = '同目录推断' }
    }
}

# 优先选用非 alpha 的内核（alpha 是测试版，防火墙规则要指向真正在跑的那个）
if ($core -and $core.Path -match '-alpha') {
    $dir = Split-Path $core.Path -Parent
    foreach ($pref in @('verge-mihomo.exe','mihomo.exe')) {
        $c = Join-Path $dir $pref
        if (Test-Path $c) {
            $core = [pscustomobject]@{ Kind = 'core'; Path = $c; Source = '同目录(优先非 alpha)' }
            break
        }
    }
}
$result = [pscustomobject]@{
    GuiExe    = if ($gui)  { $gui.Path }  else { $null }
    CoreExe   = if ($core) { $core.Path } else { $null }
    ConfigDir = $cfg
    MergeYaml = if ($cfg) { Join-Path $cfg 'profiles\Merge.yaml' } else { $null }
    ScriptJs  = if ($cfg) { Join-Path $cfg 'profiles\Script.js' } else { $null }
    All       = $found
}

if ($Json) {
    $result | ConvertTo-Json -Depth 4
    exit 0
}

Write-Host ''
Write-Host '=============== 定位结果 ===============' -ForegroundColor Cyan
foreach ($h in $found) {
    Write-Host ("  [{0,-4}] {1}" -f $h.Kind, $h.Path)
    Write-Host ("          来源: {0}" -f $h.Source) -ForegroundColor DarkGray
}
if (-not $found) {
    Write-Host '  没找到。请用 -Pick 从桌面图标选，或手动指定 -ConfigDir。' -ForegroundColor Yellow
}

Write-Host ''
Write-Host ("  GUI  : {0}" -f $(if ($result.GuiExe)    { $result.GuiExe }    else { '未找到' }))
Write-Host ("  内核 : {0}" -f $(if ($result.CoreExe)   { $result.CoreExe }   else { '未找到' }))
Write-Host ("  配置 : {0}" -f $(if ($result.ConfigDir) { $result.ConfigDir } else { '未找到' }))
Write-Host ("  Merge: {0}" -f $(if ($result.MergeYaml) { $result.MergeYaml } else { '未找到' }))
Write-Host '=======================================' -ForegroundColor Cyan
Write-Host ''

# 供其他脚本直接引用：
#   $info = & .\Find-Clash.ps1 -Json | ConvertFrom-Json
#   $info.GuiExe / $info.CoreExe / $info.ConfigDir
