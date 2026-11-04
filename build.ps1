#requires -Version 5.1
<#
.SYNOPSIS
    将 Clash WiFi Kit 打包为可执行程序。

.DESCRIPTION
    默认生成目录模式（推荐）：
        dist\ClashWiFiKit\ClashWiFiKit.exe
        dist\ClashWiFiKit\_internal\...

    加 -OneFile 生成单文件模式：
        dist\ClashWiFiKit.exe

    单文件模式依赖在临时目录解压，若运行环境限制临时目录写入会失败
    （报错“Could not create temporary directory!”）。因此默认使用目录模式。

    随包资源：clash-watchdog.ps1、Find-Clash.ps1、clash-wifi.ps1、README.md
    运行时释放到 exe 所在目录；该目录不可写时释放到 %LOCALAPPDATA%\ClashWiFiKit。

.EXAMPLE
    .\build.ps1
    .\build.ps1 -OneFile
    .\build.ps1 -Reinstall
#>
param(
    [switch]$OneFile,
    [switch]$Reinstall
)

$ErrorActionPreference = 'Stop'
$ws = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ws

# ---------------------------------------------------------------- Python
$py = $null
foreach ($c in @('python', 'py')) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    $cand = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
    if (Test-Path $cand) { $py = $cand }
}
if (-not $py) { throw '未找到 Python 3.9 及以上版本' }
Write-Host "Python：$py"

# ---------------------------------------------------------------- 依赖
$has = $false
try { & $py -c "import PyInstaller" 2>$null; $has = ($LASTEXITCODE -eq 0) } catch { }
if ($Reinstall -or -not $has) {
    Write-Host '安装 PyInstaller ...'
    & $py -m pip install --upgrade --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 安装失败' }
}

# ---------------------------------------------------------------- 资源
$assets = @('clash-watchdog.ps1', 'Find-Clash.ps1', 'clash-wifi.ps1', 'README.md')
$missing = $assets | Where-Object { -not (Test-Path (Join-Path $ws $_)) }
if ($missing) { throw ("缺少随包资源：" + ($missing -join '、')) }

# ---------------------------------------------------------------- 打包参数
$args = @('-m', 'PyInstaller', '--noconfirm', '--windowed', '--name', 'ClashWiFiKit')

if ($OneFile) {
    $args += '--onefile'
    # 强制把解压目录放到用户配置目录，规避受限临时目录
    $args += @('--runtime-tmpdir', '%LOCALAPPDATA%\ClashWiFiKit\tmp')
} else {
    $args += '--onedir'
}

foreach ($a in $assets) {
    # 必须使用绝对路径：spec 位于 build 目录时相对路径会被错误解析
    $args += @('--add-data', "$ws\$a;.")
}

$args += @(
    '--distpath', "$ws\dist",
    '--workpath', "$ws\build",
    '--specpath', "$ws\build",
    "$ws\clash-wifi-gui.py"
)

Write-Host ("开始打包（{0} 模式）..." -f $(if ($OneFile) { '单文件' } else { '目录' }))
& $py @args
if ($LASTEXITCODE -ne 0) { throw "打包失败，退出码 $LASTEXITCODE" }

# ---------------------------------------------------------------- 结果
Write-Host ''
if ($OneFile) {
    $exe = Join-Path $ws 'dist\ClashWiFiKit.exe'
    if (-not (Test-Path $exe)) { throw '未生成 exe' }
    $item = Get-Item $exe
    Write-Host ("完成：{0}" -f $item.FullName) -ForegroundColor Green
    Write-Host ("大小：{0:N2} MB" -f ($item.Length / 1MB))
}
else {
    $dir = Join-Path $ws 'dist\ClashWiFiKit'
    $exe = Join-Path $dir 'ClashWiFiKit.exe'
    if (-not (Test-Path $exe)) { throw '未生成 exe' }
    $total = (Get-ChildItem $dir -Recurse -File | Measure-Object Length -Sum).Sum
    Write-Host ("完成：{0}" -f $exe) -ForegroundColor Green
    Write-Host ("整包大小：{0:N1} MB" -f ($total / 1MB))
    Write-Host '分发时需整个 ClashWiFiKit 目录一并复制'
}

Write-Host ''
Write-Host '首次运行会在 exe 同目录释放 PowerShell 脚本，请确保该目录可写。'
