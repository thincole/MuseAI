@echo off
chcp 65001 > nul
title MuseAI Video Studio Pro - Đang Khởi Động...

cd /d "%~dp0"

echo ====================================================================
echo         MuseAI Video Studio Pro - Phần Mềm Tạo Video AI
echo                  Phiên Bản Desktop Windows
echo ====================================================================
echo.

:: Tìm đường dẫn Python 3 (dùng goto để tránh lỗi %errorlevel% bị expand sớm trong khối ngoặc)
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
echo Vui long cai dat Python 3.10 tro len va tich vao "Add Python to PATH".
echo Nhan phim bat ky de thoat...
pause > nul
exit /b 1

:py_found
echo [*] Su dung trinh thuc thi Python: %PY_EXE% %PY_ARGS%
echo [*] Khoi dong ung dung Desktop (nhat ky phien truoc duoc giu o log.txt.1)...
echo.

"%PY_EXE%" %PY_ARGS% desktop_app.py

if %errorlevel% neq 0 (
    echo.
    echo [THONG BAO] Ung dung da dung lai voi ma loi %errorlevel%.
    pause
)
