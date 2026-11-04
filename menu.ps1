#requires -Version 5.1
# Clash WiFi Kit 菜单（中文界面放在 .ps1 里，避免 cmd 的代码页问题）
$ErrorActionPreference = 'Continue'
$HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
$PY = $null
foreach ($c in @('python', 'py')) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $PY = $c; break }
}
if (-not $PY) {
    $cand = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
    if (Test-Path $cand) { $PY = $cand }
}

function Show-Menu {
    Clear-Host
    Write-Host ''
    Write-Host '  ============================================================' -ForegroundColor Cyan
    Write-Host '       Clash 只走 WiFi  ·  工具箱' -ForegroundColor Cyan
    Write-Host '  ============================================================' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '   1  定位 Clash 路径（自动查找安装位置 / 内核 / 配置目录）'
    Write-Host '   2  体检（网卡 · 路由 · 配置 · 坑点，一次全查）'
    Write-Host '   3  写入 interface-name（自动探测无线网卡名）'
    Write-Host '   4  验证 · 出口分布（谁走 WiFi、谁走有线）'
    Write-Host '   5  验证 · 断 WiFi 实测 fail-closed'
    Write-Host '   6  启动看门狗（WiFi 掉线自动关代理 + 退 Clash）'
    Write-Host '   7  钉住网卡优先级（有线 10 / WiFi 9999）'
    Write-Host '   8  从备份还原配置'
    Write-Host '   9  打开说明文档'
    Write-Host '   0  退出'
    Write-Host ''
    if (-not $PY) {
        Write-Host '   注意：没找到 Python，第 3 / 8 项不可用。' -ForegroundColor Yellow
    }
    Write-Host ''
}

function Pause-Back {
    Write-Host ''
    Read-Host '  按回车返回菜单' | Out-Null
}

function Run-Ps([string]$file, [string[]]$extra) {
    $p = Join-Path $HERE $file
    if (-not (Test-Path $p)) { Write-Host ("  缺少文件: {0}" -f $file) -ForegroundColor Red; return }
    & $p @extra
}

while ($true) {
    Show-Menu
    $ch = Read-Host '  请选择'

    switch ($ch) {
        '1' { Run-Ps 'Find-Clash.ps1' @(); Pause-Back }
        '2' { Run-Ps 'clash-wifi.ps1' @('-Action', 'inspect'); Pause-Back }
        '3' {
            $bat = Join-Path $HERE 'add-interface-name.bat'
            if (Test-Path $bat) { & $bat } else { Write-Host '  缺少 add-interface-name.bat' -ForegroundColor Red }
            Pause-Back
        }
        '4' { Run-Ps 'clash-wifi.ps1' @('-Action', 'split'); Pause-Back }
        '5' {
            Write-Host ''
            Write-Host '  这一步会真的断开你的 WiFi，约 10~30 秒。' -ForegroundColor Yellow
            $ok = Read-Host '  确认请输入 y'
            if ($ok -eq 'y') { Run-Ps 'clash-wifi.ps1' @('-Action', 'drill', '-Apply') }
            else { Write-Host '  已取消' }
            Pause-Back
        }
        '6' {
            $wd = Join-Path $HERE 'clash-watchdog.ps1'
            if (Test-Path $wd) {
                Start-Process -FilePath 'powershell.exe' -ArgumentList @(
                    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $wd
                ) | Out-Null
                Write-Host ''
                Write-Host '  看门狗已在新窗口启动，那个窗口别关。' -ForegroundColor Green
                Write-Host '  想加参数（如 -KeepClash）就直接运行 clash-watchdog.cmd' -ForegroundColor DarkGray
            }
            else { Write-Host '  缺少 clash-watchdog.ps1' -ForegroundColor Red }
            Pause-Back
        }
        '7' {
            Write-Host ''
            Write-Host '  需要管理员权限。' -ForegroundColor Yellow
            Run-Ps 'clash-wifi.ps1' @('-Action', 'pin', '-Apply')
            Pause-Back
        }
        '8' {
            $bat = Join-Path $HERE 'add-interface-name.bat'
            if (Test-Path $bat) { & $bat '--restore' } else { Write-Host '  缺少 add-interface-name.bat' -ForegroundColor Red }
            Pause-Back
        }
        '9' {
            $md = Join-Path $HERE 'README.md'
            if (Test-Path $md) { Start-Process notepad.exe $md | Out-Null } else { Write-Host '  缺少 README.md' -ForegroundColor Red }
            Pause-Back
        }
        '0' { return }
        default { }
    }
}
