@echo off
chcp 65001 >nul
title Build LiveRecorder.exe
cd /d "%~dp0"

REM Use real Python (bypass Microsoft Store stub)
set PYTHON=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
set PIP=%LOCALAPPDATA%\Programs\Python\Python311\Scripts\pip.exe

echo.
echo ============================================
echo   Building LiveRecorder.exe
echo ============================================
echo.

echo [1/3] Installing PyInstaller...
"%PIP%" install pyinstaller -q

echo [2/3] Building single .exe (no console)...
echo.
REM PyInstaller must run from the PARENT directory because
REM recorder_server.py and recorder_panel.html are there.
cd /d "%~dp0.."

"%PYTHON%" -m PyInstaller --onefile --noconsole ^
    --disable-windowed-traceback ^
    --paths "." ^
    --add-data "luzhi\recorder_panel.html;." ^
    --name "LiveRecorder" ^
    --distpath "luzhi\dist" ^
    luzhi\desktop_app.py

echo.
echo [3/3] Copying to Desktop...
copy /Y "luzhi\dist\LiveRecorder.exe" "%USERPROFILE%\Desktop\LiveRecorder.exe" >nul

echo.
echo ============================================
echo   Build complete!
echo   Desktop: LiveRecorder.exe
echo ============================================
echo.
echo   Double-click LiveRecorder.exe on Desktop
echo   No terminal - only the control panel window
echo.
pause
