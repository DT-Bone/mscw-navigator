@echo off
rem MSCW Navigator: double-click to install it (the first time), update it and start it.
rem It lives in %LOCALAPPDATA%\MSCW-Navigator, with its own copy of Python, and a shortcut on the desktop.
setlocal
set "REPO=DT-Bone/mscw-navigator"
set "BRANCH=main"
set "APP=%LOCALAPPDATA%\MSCW-Navigator"
if defined MSCW_APP set "APP=%MSCW_APP%"

if not exist "%APP%\runtime\pythonw.exe" goto install
if not exist "%APP%\launcher.py" goto install

:start
rem a replaced copy of Python that was still in use last time
if exist "%APP%\runtime.old" rmdir /s /q "%APP%\runtime.old" 2>nul
if exist "%APP%\runtime.new\pythonw.exe" call :swap
rem (MSCW_APP and MSCW_LAUNCHER_ARGS are for testing, e.g. MSCW_LAUNCHER_ARGS=--test)
start "" "%APP%\runtime\pythonw.exe" "%APP%\launcher.py" %MSCW_LAUNCHER_ARGS%
exit /b

:install
echo Installing MSCW Navigator...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; try { [Net.ServicePointManager]::SecurityProtocol = 'Tls12'; $ProgressPreference = 'SilentlyContinue'; New-Item -ItemType Directory -Force '%APP%' | Out-Null; Invoke-WebRequest 'https://raw.githubusercontent.com/%REPO%/%BRANCH%/install.ps1' -OutFile '%APP%\install.ps1' -UseBasicParsing; & '%APP%\install.ps1' -App '%APP%' -Repo '%REPO%' -Branch '%BRANCH%' } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo.
    echo Installing MSCW Navigator failed. Check your internet connection and try again.
    pause
    exit /b 1
)
goto start

:swap
rem launcher.py downloaded a new copy of Python: switch to it (retrying while an old copy is in use)
for /l %%i in (1,1,20) do (
    if exist "%APP%\runtime.old" rmdir /s /q "%APP%\runtime.old" 2>nul
    if not exist "%APP%\runtime.old" ren "%APP%\runtime" runtime.old 2>nul && goto swapped
    ping -n 2 127.0.0.1 >nul
)
exit /b
:swapped
ren "%APP%\runtime.new" runtime
rmdir /s /q "%APP%\runtime.old" 2>nul
exit /b
