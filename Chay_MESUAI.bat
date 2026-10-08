@echo off
title MuseAI Video Studio Pro

if exist "%~dp0root\desktop_app.py" (
    cd /d "%~dp0root"
) else if exist "%~dp0desktop_app.py" (
    cd /d "%~dp0"
)

echo ====================================================================
echo         MuseAI Video Studio Pro - Video Generator AI
echo                  Desktop Windows Edition
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
echo Vui long cai dat Python 3.10 tro len va tich vao "Add Python to PATH".
echo Nhan phim bat ky de thoat...
pause > nul
exit /b 1

:py_found
echo [*] Trinh thuc thi Python: %PY_EXE% %PY_ARGS%
echo [*] Thu muc hoat dong: %CD%
echo [*] Dang khoi dong ung dung Desktop...
echo.

if not exist "desktop_app.py" (
    echo [LOI] Khong tim thay file desktop_app.py tai thu muc: %CD%
    pause
    exit /b 1
)

"%PY_EXE%" %PY_ARGS% desktop_app.py

if %errorlevel% neq 0 (
    echo.
    echo [THONG BAO] Ung dung da dung lai voi ma loi %errorlevel%.
    pause
)
