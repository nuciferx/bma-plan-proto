@echo off
setlocal
title BMA-Plan Build

echo =========================================
echo   Build BMA-Plan Portable App
echo =========================================
echo.

:: ตรวจว่ามี Python ไหม
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] ไม่พบ Python — ติดตั้งก่อนนะครับ
    pause & exit /b 1
)

:: ติดตั้ง dependencies
echo [1/3] ตรวจสอบ dependencies...
pip install -q fastapi uvicorn python-multipart pymupdf pyinstaller
echo       OK

:: ลบ build เก่า
echo [2/3] เตรียม build...
if exist "dist\BMA-Plan" rmdir /s /q "dist\BMA-Plan"
if exist "build"          rmdir /s /q "build"
if exist "BMA-Plan.spec"  del /q "BMA-Plan.spec"
echo       OK

:: Build
echo [3/3] Building... (ใช้เวลา 2-4 นาที)
echo.

pyinstaller ^
  --onedir ^
  --console ^
  --name "BMA-Plan" ^
  --collect-all pymupdf ^
  --hidden-import=fitz ^
  --hidden-import=uvicorn.logging ^
  --hidden-import=uvicorn.loops ^
  --hidden-import=uvicorn.loops.auto ^
  --hidden-import=uvicorn.protocols ^
  --hidden-import=uvicorn.protocols.http ^
  --hidden-import=uvicorn.protocols.http.auto ^
  --hidden-import=uvicorn.protocols.websockets ^
  --hidden-import=uvicorn.protocols.websockets.auto ^
  --hidden-import=uvicorn.lifespan ^
  --hidden-import=uvicorn.lifespan.on ^
  --hidden-import=fastapi ^
  --hidden-import=python_multipart ^
  --hidden-import=multipart ^
  --hidden-import=email.mime.multipart ^
  launch.py

echo.
if exist "dist\BMA-Plan\BMA-Plan.exe" (
    echo =========================================
    echo   BUILD สำเร็จ!
    echo   dist\BMA-Plan\BMA-Plan.exe
    echo.
    echo   วิธีแจก:
    echo   - zip โฟลเดอร์ dist\BMA-Plan ทั้งโฟลเดอร์
    echo   - copy ไปเครื่องอื่น unzip แล้ว run BMA-Plan.exe
    echo   - ไม่ต้องติดตั้ง Python หรืออะไรเพิ่ม
    echo =========================================
) else (
    echo =========================================
    echo   BUILD ล้มเหลว — ดู error ด้านบน
    echo =========================================
)
echo.
pause
