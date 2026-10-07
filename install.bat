@echo off
chcp 65001 > nul
title MuseAI Video Studio Pro - Cai Dat Moi Truong
cd /d "%~dp0"

echo ====================================================================
echo         MuseAI Video Studio Pro - Cai Dat Moi Truong Tu Dong
echo ====================================================================
echo.

:: 1. Tim trinh thong dich Python tren he thong
set "PY_CMD="

where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    ) else if exist "C:\Python312\python.exe" (
        set "PY_CMD=C:\Python312\python.exe"
    ) else (
        where py >nul 2>nul
        if %errorlevel% equ 0 (
            set "PY_CMD=py -3.12"
        )
    )
)

if "%PY_CMD%"=="" (
    echo ====================================================================
    echo [LOI] Khong tim thay Python tren may tinh!
    echo.
    echo Vui long tai va cai dat Python (Khuyen dung Python 3.10 - 3.12):
    echo   https://www.python.org/downloads/
    echo.
    echo * LUU Y QUAN TRONG:
    echo   Khi cai dat, nho tich chon vao o:
    echo   [v] "Add python.exe to PATH"
    echo ====================================================================
    echo.
    pause
    exit /b 1
)

echo [*] Da tim thay Python:
"%PY_CMD%" --version
echo.

:: 2. Nang cap pip
echo [*] Dang kiem tra va nang cap Pip...
"%PY_CMD%" -m pip install --upgrade pip
echo.

:: 3. Cai dat thu vien tu requirements.txt
echo [*] Dang cai dat cac thu vien phu thuoc (FastAPI, Uvicorn, PyWebView, PyQt6, DrissionPage...)...
"%PY_CMD%" -m pip install -r requirements.txt

if %errorlevel% neq 0 (
    echo.
    echo [CANH BAO] Co mot so goi cai dat gap loi! Vui long kiem tra ket noi mang va thu lai.
    pause
    exit /b %errorlevel%
)

:: 4. Khoi tao thu muc du lieu
echo.
echo [*] Dang khoi tao cac thu muc du lieu...
if not exist "data" mkdir "data"
if not exist "data\media" mkdir "data\media"
if not exist "data\downloads" mkdir "data\downloads"

:: 5. Khoi tao file .env neu chua co
if not exist ".env" (
    if exist ".env.example" (
        echo [*] Dang tao file .env tu mau .env.example...
        copy ".env.example" ".env" > nul
    )
)

echo.
echo ====================================================================
echo  [V] CAI DAT MOI TRUONG THANH CONG!
echo.
echo  Ban co the bat dau su dung:
echo    - Chay ung dung Desktop : Chay file "Chay_MESUAI.bat"
echo    - Thu hoach Cookie      : Chay file "Chay_Lay Cookie1.bat"
echo    - Day code len GitHub   : Chay file "upload-github.bat"
echo    - Cap nhat phien ban    : Chay file "update.bat"
echo ====================================================================
echo.
pause
