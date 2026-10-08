@echo off
chcp 65001 > nul
title MuseAI Video Studio Pro - Cai Dat Moi Truong
cd /d "%~dp0"

echo ====================================================================
echo         MuseAI Video Studio Pro - Cai Dat Moi Truong Tu Dong
echo ====================================================================
echo.

set "PY_CMD="

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if "%PY_CMD%"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if "%PY_CMD%"=="" if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if "%PY_CMD%"=="" if exist "C:\Python312\python.exe" set "PY_CMD=C:\Python312\python.exe"
if "%PY_CMD%"=="" where python >nul 2>nul && set "PY_CMD=python"
if "%PY_CMD%"=="" where py >nul 2>nul && set "PY_CMD=py -3.12"

if not "%PY_CMD%"=="" goto :has_python

echo ====================================================================
echo [LOI] Khong tim thay Python tren may tinh!
echo.
echo Vui long tai va cai dat Python [Khuyen dung ban 3.10 - 3.12]:
echo   https://www.python.org/downloads/
echo.
echo * LUU Y QUAN TRONG:
echo   Khi cai dat, nho tich chon vao o: [v] Add python.exe to PATH
echo ====================================================================
echo.
pause
exit /b 1

:has_python
echo [*] Su dung trinh thuc thi Python: %PY_CMD%
"%PY_CMD%" --version
echo.

echo [*] Dang kiem tra va nang cap Pip...
"%PY_CMD%" -m pip install --upgrade pip
echo.

echo [*] Dang cai dat cac thu vien phu thuoc (FastAPI, Uvicorn, PyWebView, PyQt6, DrissionPage...)...
"%PY_CMD%" -m pip install -r requirements.txt

echo.
echo [*] Dang khoi tao cac thu muc du lieu...
if not exist "data" mkdir "data"
if not exist "data\media" mkdir "data\media"
if not exist "data\downloads" mkdir "data\downloads"

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
