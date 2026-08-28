@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: double-click setup_windows.bat once.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" rotation_screener.py --config config.example.json
if errorlevel 1 (
  echo.
  echo Screening failed. Cached data was also unavailable. See the message above.
) else (
  echo.
  echo Done: test_output\data.test.json
)
pause


