@echo off
chcp 65001 > nul
cd /d "%~dp0"
title MuseAI Cookie Harvester Pro - Giao Dien Do Hoa

set "PY_CMD="
where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
) else (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
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
    echo Vui long cai dat Python 3.10 tro len.
    pause
    exit /b 1
)

echo [*] Dang khoi dong Giao Dien Do Hoa MuseAI Cookie Harvester Pro...
start "" "%PY_CMD%" gui_auto_login.py
exit /b 0
