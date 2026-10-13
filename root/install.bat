@echo off
chcp 65001 > nul
title MuseAI Video Studio Pro - Cai Dat Moi Truong
cd /d "%~dp0"

echo ====================================================================
echo         MuseAI Video Studio Pro - Cai Dat Moi Truong Tu Dong
echo ====================================================================
echo.

set "PY_VER=3.12.10"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "PY_SETUP=%TEMP%\python-%PY_VER%-amd64.exe"
set "PY_CMD="

call :find_python
if not "%PY_CMD%"=="" goto :has_python

echo [!] Chua co Python tren may nay (hoac chi co loi tat gia cua Windows Store).
echo [*] Dang tu dong cai dat Python %PY_VER% (ban 64-bit, cai cho nguoi dung hien tai, tu them vao PATH)...
echo.

REM --- Cach 1: tai bo cai tu python.org va cai im lang ---
echo [*] Dang tai bo cai Python tu python.org ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -UseBasicParsing -Uri '%PY_URL%' -OutFile '%PY_SETUP%' } catch { Write-Host $_.Exception.Message; exit 1 }"
if exist "%PY_SETUP%" (
    echo [*] Dang cai dat Python, vui long cho 1-3 phut ...
    "%PY_SETUP%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1 Include_test=0 Include_doc=0 Shortcuts=0
    del /q "%PY_SETUP%" > nul 2>nul
)
call :find_python
if not "%PY_CMD%"=="" goto :python_ready

REM --- Cach 2: dung winget neu cach 1 that bai ---
where winget >nul 2>nul
if not errorlevel 1 (
    echo [*] Thu cai Python bang winget ...
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    call :find_python
    if not "%PY_CMD%"=="" goto :python_ready
)

echo ====================================================================
echo [LOI] Khong tu cai duoc Python.
echo.
echo Vui long cai thu cong:
echo   1. Tai Python 3.12 (64-bit): https://www.python.org/downloads/
echo   2. Khi cai, TICH vao o: [v] Add python.exe to PATH
echo   3. Windows Settings - Apps - Advanced app settings - App execution aliases:
echo      tat 2 dong python.exe va python3.exe (App Installer)
echo   4. Chay lai file install.bat nay
echo ====================================================================
echo.
pause
exit /b 1

:python_ready
echo [V] Da cai xong Python.
echo.

:has_python
echo [*] Su dung trinh thuc thi Python: %PY_CMD%
"%PY_CMD%" --version
echo.

if /i "%~1"=="--check" exit /b 0

echo [*] Dang kiem tra va nang cap Pip...
"%PY_CMD%" -m pip install --upgrade pip
if errorlevel 1 goto :pip_fail
echo.

echo [*] Dang cai dat cac thu vien phu thuoc (FastAPI, Uvicorn, PyWebView, PyQt6, DrissionPage...)...
"%PY_CMD%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [!] Cai thu vien that bai lan 1, thu lai lan 2 ...
    "%PY_CMD%" -m pip install -r requirements.txt
    if errorlevel 1 goto :pip_fail
)

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
exit /b 0

:pip_fail
echo.
echo ====================================================================
echo [LOI] Cai dat thu vien KHONG thanh cong (xem thong bao do o tren).
echo   - Kiem tra ket noi Internet roi chay lai install.bat
echo   - Neu van loi, chup man hinh nay gui cho nguoi ho tro
echo ====================================================================
echo.
pause
exit /b 1

REM ====================================================================
REM  Tim Python THAT: chay thu "--version", bo qua loi tat gia cua Windows Store
REM ====================================================================
:find_python
set "PY_CMD="
call :try_py "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try_py "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try_py "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :try_py "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
call :try_py "C:\Python312\python.exe"
call :try_py "C:\Program Files\Python312\python.exe"
call :try_py "python"
exit /b 0

:try_py
if not "%PY_CMD%"=="" exit /b 0
"%~1" --version >nul 2>nul
if not errorlevel 1 set "PY_CMD=%~1"
exit /b 0
