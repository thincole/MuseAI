@echo off
chcp 65001 > nul
title MuseAI Video Studio Pro - Đang Khởi Động...

cd /d "%~dp0"

echo ====================================================================
echo         MuseAI Video Studio Pro - Phần Mềm Tạo Video AI
echo                  Phiên Bản Desktop Windows
echo ====================================================================
echo.

:: Tìm đường dẫn Python 3
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
    echo [LOI] Khong tim thay Python tren he thong!
    echo Vui long cai dat Python 3.10 tro len va tich vao "Add Python to PATH".
    echo Nhan phim bat ky de thoat...
    pause > nul
    exit /b 1
)

echo [*] Su dung trinh thuc thi Python: %PY_CMD%
echo [*] Xoa trang nhat ky cu va khoi dong ung dung Desktop...
if exist log.txt del /f /q log.txt >nul 2>&1
echo.

"%PY_CMD%" desktop_app.py

if %errorlevel% neq 0 (
    echo.
    echo [THONG BAO] Ung dung da dung lai voi ma loi %errorlevel%.
    pause
)
