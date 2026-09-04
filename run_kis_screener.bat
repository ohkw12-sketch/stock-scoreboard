@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: double-click setup_windows.bat once.
  pause
  exit /b 1
)
if "%KIS_APP_KEY%"=="" (
  echo KIS_APP_KEY is not set. Add the KIS Developers app key to Windows environment variables.
  pause
  exit /b 1
)
if "%KIS_APP_SECRET%"=="" (
  echo KIS_APP_SECRET is not set. Add the KIS Developers app secret to Windows environment variables.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" refresh_all.py --config config.kis.example.json
if errorlevel 1 (
  echo.
  echo Screening failed. Check test_output\consensus_failures.test.json and the message above.
) else (
  echo.
  echo Done: test_output\data.test.json
)
pause
