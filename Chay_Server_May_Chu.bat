@echo off
title MuseAI Central Server - Tailscale Account Hub

if exist "%~dp0root\app.py" (
    cd /d "%~dp0root"
) else if exist "%~dp0app.py" (
    cd /d "%~dp0"
)

echo ====================================================================
echo        MUSEAI VIDEO STUDIO - MAY CHU QUAN LY TAI KHOAN TAP TRUNG
echo             Central Account Hub qua mang Tailscale
echo ====================================================================
echo.

:: Tim Python 3
set "PY_EXE="
set "PY_ARGS="

where python >nul 2>nul
if not errorlevel 1 (
    set "PY_EXE=python"
    goto :py_found
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :py_found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    goto :py_found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :py_found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto :py_found
)
if exist "C:\Python312\python.exe" (
    set "PY_EXE=C:\Python312\python.exe"
    goto :py_found
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PY_EXE=py"
    set "PY_ARGS=-3"
    goto :py_found
)

echo [LOI] Khong tim thay Python tren he thong!
echo Vui long cai dat Python 3.10 tro len.
pause
exit /b 1

:py_found
echo [*] Trinh thuc thi Python: %PY_EXE% %PY_ARGS%
echo [*] Thu muc hoat dong: %CD%
echo.

:: Tu dong giai phong cong 18610 neu co tien trinh cu dang chiem cong
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":18610 " ^| findstr "LISTENING"') do (
    echo [*] Dang dong tien trinh Server cu tren cong 18610 ^(PID: %%a^)...
    taskkill /F /PID %%a >nul 2>nul
)

echo [*] Dang khoi chay May Chu API tren cong 18610...
echo [*] Cac may tram ket noi qua Tailscale: http://100.79.170.67:18610
echo [*] Trang quan tri Web: http://127.0.0.1:18610/admin
echo.

"%PY_EXE%" %PY_ARGS% -m uvicorn app:app --host 0.0.0.0 --port 18610 --access-log

pause
exit /b 0
