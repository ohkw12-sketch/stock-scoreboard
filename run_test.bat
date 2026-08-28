@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" rotation_screener.py --mode sample
) else (
  py -3 rotation_screener.py --mode sample
)
if errorlevel 1 (
  echo.
  echo Test failed. See the message above.
) else (
  echo.
  echo Done: test_output\data.test.json
)
pause


