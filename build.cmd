@echo off
setlocal
set "HERE=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%build.ps1" %*
if errorlevel 1 (
    echo.
    echo   Build failed.
    pause
    exit /b 1
)
echo.
pause