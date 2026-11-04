@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
set "PYTHONIOENCODING=utf-8"
set "HERE=%~dp0"
set "PY="

rem --- 1) python on PATH
where python >nul 2>&1 && set "PY=python"

rem --- 2) py launcher
if not defined PY (
    where py >nul 2>&1 && set "PY=py"
)

rem --- 3) DSH bundled runtime
if not defined PY (
    set "CAND=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if exist "!CAND!" set "PY=!CAND!"
)

if not defined PY (
    echo.
    echo   Python 3 not found.
    echo   Install it, or run the .py file with any python 3.8+.
    echo.
    pause
    exit /b 1
)

rem --- run
"%PY%" "%HERE%add-interface-name.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo   Finished.
) else (
    echo   Failed, exit code %RC%.
)
echo.
pause
exit /b %RC%
