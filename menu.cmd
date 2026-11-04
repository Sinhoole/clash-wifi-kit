@echo off
setlocal
title Clash WiFi Kit

rem Keep this file ASCII-only: cmd.exe mangles UTF-8 in .bat files.
rem The Chinese UI lives in menu.ps1 instead.

if not exist "%~dp0menu.ps1" (
    echo.
    echo   menu.ps1 not found in %~dp0
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0menu.ps1"
endlocal
