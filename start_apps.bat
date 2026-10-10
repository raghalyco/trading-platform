@echo off
setlocal

set "REPO=%~dp0"
if not exist "%REPO%logs" mkdir "%REPO%logs"

echo ==== %date% %time% - START ==== >> "%REPO%logs\signal_engine.log"
echo ==== %date% %time% - START ==== >> "%REPO%logs\scanner.log"

echo Starting SignalEngine (port 8000)...
cd /d "%REPO%signal_engine"
start "SignalEngine" /min cmd /c ""%REPO%signal_engine\.venv\Scripts\python.exe" -m app.api.main >> "%REPO%logs\signal_engine.log" 2>&1"

echo Starting Scanner (port 5000)...
cd /d "%REPO%scanner"
start "Scanner" /min cmd /c ""%REPO%scanner\.venv\Scripts\python.exe" app.py >> "%REPO%logs\scanner.log" 2>&1"

cd /d "%REPO%"
echo Done. SignalEngine -^> http://localhost:8000  Scanner -^> http://localhost:5000
echo Logs: %REPO%logs\signal_engine.log and %REPO%logs\scanner.log
echo Remember to complete today's Kite login from your phone via the /admin/{token}/login page.
