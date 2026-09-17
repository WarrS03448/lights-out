@echo off
setlocal
rem Runs the built hub exactly as a player would: against the LIVE catalogue on Railway (no --local).
cd /d "%~dp0.."
set "EXE=dist\LightsOut\LightsOut.exe"
if not exist "%EXE%" (
  echo No %EXE% yet - run hub\build_hub.bat or release\publish_hub.bat first.
  pause & exit /b 1
)
start "" "%EXE%"
