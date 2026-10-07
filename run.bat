@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

set "BOT_DIR=D:\AutoBot"
set "CDP_PORT=9222"
set "EDGE_PROFILE_DIR=D:\AutoBot\edge_bot_profile"
set "EDGE_PROFILE_NAME=Default"
set "EDGE_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

if not exist "%BOT_DIR%\main_script.py" (
    echo [LOI] Khong tim thay project tai:
    echo       %BOT_DIR%\main_script.py
    echo Hay copy source code vao D:\AutoBot truoc khi chay.
    pause
    exit /b 1
)

cd /d "%BOT_DIR%"
title Browser Bot - Edge CDP %CDP_PORT%

echo.
echo ============================================
echo   BROWSER BOT - PRODUCTION
echo   Project : %BOT_DIR%
echo   CDP     : 127.0.0.1:%CDP_PORT%
echo   Profile : %EDGE_PROFILE_DIR%
echo ============================================
echo.

if exist ".venv312\Scripts\activate.bat" goto :activate_venv312
if exist "venv\Scripts\activate.bat" goto :activate_venv

echo [LOI] Khong tim thay virtual environment.
echo Hay chay setup.bat truoc.
pause
exit /b 1

:activate_venv312
echo [*] Kich hoat .venv312...
call ".venv312\Scripts\activate.bat"
goto :venv_ready

:activate_venv
echo [*] Kich hoat venv...
call "venv\Scripts\activate.bat"

goto :venv_ready

:venv_ready
if not exist ".env" (
    echo [LOI] Khong tim thay file .env tai:
    echo       %BOT_DIR%\.env
    echo Hay tao .env va dien API_ID, API_HASH, ALERT_BOT_TOKEN.
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs"
if not exist "%EDGE_PROFILE_DIR%" mkdir "%EDGE_PROFILE_DIR%"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

if not exist "%EDGE_EXE%" (
    set "EDGE_EXE_ALT=C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    if exist "!EDGE_EXE_ALT!" set "EDGE_EXE=!EDGE_EXE_ALT!"
)

if not exist "%EDGE_EXE%" (
    echo [LOI] Khong tim thay Microsoft Edge.
    echo Da kiem tra hai duong dan mac dinh trong C:\Program Files.
    pause
    exit /b 1
)

echo [*] Kiem tra Edge CDP tai 127.0.0.1:%CDP_PORT%...
powershell -NoProfile -Command "try { $c = New-Object System.Net.Sockets.TcpClient('127.0.0.1', %CDP_PORT%); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :edge_ready

echo [*] Edge chua mo. Dang khoi dong Edge CDP...
start "Edge CDP %CDP_PORT%" "%EDGE_EXE%" --remote-debugging-port=%CDP_PORT% --remote-debugging-address=127.0.0.1 --remote-allow-origins=http://localhost:%CDP_PORT%,http://127.0.0.1:%CDP_PORT% --disable-blink-features=AutomationControlled --user-data-dir="%EDGE_PROFILE_DIR%" --profile-directory="%EDGE_PROFILE_NAME%" --disable-background-networking --disable-sync --disable-translate --disable-component-update --disable-domain-reliability --disable-client-side-phishing-detection --disable-default-apps --no-first-run --no-default-browser-check --mute-audio --disable-background-timer-throttling --disable-renderer-backgrounding --disable-backgrounding-occluded-windows --disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,AutofillServerCommunication,CalculateNativeWinOcclusion

echo [*] Cho Edge khoi dong...
set /a RETRY=0

:wait_loop
set /a RETRY+=1
if %RETRY% GEQ 20 goto :edge_fail
timeout /t 2 /nobreak >nul
powershell -NoProfile -Command "try { $c = New-Object System.Net.Sockets.TcpClient('127.0.0.1', %CDP_PORT%); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 goto :wait_loop

goto :edge_ready

:edge_fail
echo.
echo [LOI] Khong the ket noi Edge tai 127.0.0.1:%CDP_PORT%.
echo Hay dong tat ca cua so Edge roi chay lai file nay.
pause
exit /b 1

:edge_ready
echo [OK] Edge CDP da san sang.
echo [*] Khoi dong browser bot...
echo.

python main_script.py
set "BOT_EXIT=%ERRORLEVEL%"

echo.
echo Bot da dung - ma loi: %BOT_EXIT%
pause
exit /b %BOT_EXIT%
