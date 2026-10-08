@echo off
chcp 65001 > nul
cd /d "%~dp0"
title MuseAI Cookie Harvester Pro - Giao Dien Do Hoa

set "PY_EXE="
set "PY_ARGS="
where python >nul 2>nul
if not errorlevel 1 (
    set "PY_EXE=python"
    goto :py_found
)
for %%V in (312 313 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :py_found
    )
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
echo [*] Dang khoi dong Giao Dien Do Hoa MuseAI Cookie Harvester Pro...
start "" "%PY_EXE%" %PY_ARGS% gui_auto_login.py
exit /b 0
