@echo off
chcp 65001 >nul
setlocal

echo.
echo ============================================
echo   SETUP ENV - BOT TELEGRAM + EDGE CDP 9222
echo ============================================
echo.

python --version 2>nul || (
    echo [LOI] Python not found in PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)

if not exist .venv312 (
    echo [1/4] Creating virtualenv...
    python -m venv .venv312
) else (
    echo [1/4] .venv312 already exists
)

echo [2/4] Activating virtualenv...
call .venv312\Scripts\activate.bat

echo [3/4] Upgrading pip/setuptools/wheel...
python -m pip install --upgrade pip setuptools wheel

echo [4/4] Installing Python packages...
python -m pip install -r requirements.txt

echo.
echo ============================================
echo   SETUP COMPLETE
echo ============================================
echo.
echo 1) MM88/RR88/XX88/O8/GG88 dung Edge that qua CDP port 9222.
echo 2) OCR dung RapidOCR (ONNXRuntime) - da cai tu dong.
echo 3) Neu xu ly video: cai FFmpeg va them vao PATH.
echo 4) Sau setup: chay run.bat de khoi dong bot.
echo.
pause
