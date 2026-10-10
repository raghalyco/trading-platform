@echo off
setlocal

set "REPO=%~dp0"

echo Stopping SignalEngine and Scanner...
taskkill /FI "WINDOWTITLE eq SignalEngine*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Scanner*" /T /F >nul 2>&1

if exist "%REPO%logs\signal_engine.log" echo ==== %date% %time% - STOPPED ==== >> "%REPO%logs\signal_engine.log"
if exist "%REPO%logs\scanner.log" echo ==== %date% %time% - STOPPED ==== >> "%REPO%logs\scanner.log"

echo Done.
