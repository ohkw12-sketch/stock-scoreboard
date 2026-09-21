@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: double-click setup_windows.bat once.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" forecast_report_engine.py --start-date 2026-01-01 --incremental %*
if errorlevel 1 (
  echo.
  echo Forecast collection failed. Check test_output\forecast_engine\run_report.json and extraction_failures.json.
) else (
  echo.
  echo Done: test_output\forecast_engine\forecast_consensus.csv
)
pause
