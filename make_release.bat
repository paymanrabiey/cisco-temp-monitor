@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM فقط اسنپ‌شات سورس + EXE موجود را در releases می‌گذارد (بدون rebuild)
call "%~dp0build.bat"
