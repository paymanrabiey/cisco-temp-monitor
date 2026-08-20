@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Read VERSION file (first line)
set VER=1.0.0
for /f "usebackq tokens=* delims=" %%A in ("VERSION") do (
  set VER=%%A
  goto :have_ver
)
:have_ver
set VER=%VER: =%
echo === Cisco Temp Monitor  v%VER% ===

echo.
echo === نصب وابستگی‌ها ===
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Python پیدا نشد. لطفاً Python 3.10+ نصب کنید.
  pause
  exit /b 1
)

echo.
echo === ساخت فایل EXE ===
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "CiscoTempMonitor" ^
  --hidden-import=pysnmp.smi.mibs ^
  --hidden-import=pysnmp.hlapi ^
  --hidden-import=paramiko ^
  --collect-all pysnmp ^
  --collect-all pyasn1 ^
  --collect-all paramiko ^
  main.py

if errorlevel 1 (
  echo ساخت EXE ناموفق بود.
  pause
  exit /b 1
)

if not exist "dist" mkdir dist
copy /Y "VERSION" "dist\VERSION" >nul 2>nul
if exist "devices.json" copy /Y "devices.json" "dist\devices.json" >nul
if not exist "dist\config" mkdir "dist\config"
if exist "config\.gitkeep" copy /Y "config\.gitkeep" "dist\config\.gitkeep" >nul

echo.
echo === بکاپ نسخه در releases\v%VER% ===
set REL=releases\v%VER%
if exist "%REL%" (
  echo پوشه نسخه از قبل هست — بازنویسی می‌شود.
  rmdir /s /q "%REL%"
)
mkdir "%REL%"
mkdir "%REL%\SOURCE"

copy /Y "dist\CiscoTempMonitor.exe" "%REL%\CiscoTempMonitor.exe" >nul
copy /Y "dist\CiscoTempMonitor.exe" "dist\CiscoTempMonitor-v%VER%.exe" >nul
copy /Y "VERSION" "%REL%\VERSION" >nul
if exist "devices.json" copy /Y "devices.json" "%REL%\devices.json" >nul
if exist "requirements.txt" copy /Y "requirements.txt" "%REL%\SOURCE\" >nul

for %%F in (main.py config_backup.py discover.py snmp_inventory.py snmp_temp.py run.bat build.bat make_release.bat) do (
  if exist "%%F" copy /Y "%%F" "%REL%\SOURCE\" >nul
)

(
  echo Cisco Temp Monitor v%VER%
  echo Built: %DATE% %TIME%
  echo.
  echo Deploy:
  echo   1^) Copy CiscoTempMonitor.exe to the monitoring server
  echo   2^) Keep devices.json next to the EXE ^(same folder^)
  echo   3^) Config backups appear in .\config\ next to the EXE
) > "%REL%\NOTES.txt"

echo.
echo آماده:
echo   dist\CiscoTempMonitor.exe
echo   dist\CiscoTempMonitor-v%VER%.exe
echo   releases\v%VER%\
echo.
pause
