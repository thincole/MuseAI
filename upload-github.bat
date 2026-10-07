@echo off
chcp 65001 > nul
title MuseAI - Day Code Len GitHub
cd /d "%~dp0"

:: Tim Python tren he thong
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
    echo [LOI] Khong tim thay trinh thuc thi Python tren he thong!
    echo Vui long cai dat Python 3.10 tro len hoac chay install.bat truoc.
    echo ====================================================================
    echo.
    pause
    exit /b 1
)

:: Khoi chay tool upload bang Python
"%PY_CMD%" tools/upload_github.py

if %errorlevel% neq 0 (
    echo.
    echo [THONG BAO] Tien trinh ket thuc voi ma loi %errorlevel%.
)

echo.
pause
