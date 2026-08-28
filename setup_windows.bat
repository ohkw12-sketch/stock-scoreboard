@echo off
setlocal
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo.
echo Setup complete. Double-click run_screener.bat for live data.
pause
exit /b 0
:fail
echo.
echo Setup failed. Check Python 3 and your internet connection.
pause
exit /b 1


