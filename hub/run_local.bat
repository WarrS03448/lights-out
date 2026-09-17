@echo off
setlocal
rem Runs the built hub against THIS checkout: server\public\catalogue.json with every pack_url
rem pointed at server\public\packs\*.zip. No server, no environment variables, no editing.
cd /d "%~dp0.."
set "EXE=dist\LightsOut\LightsOut.exe"
if not exist "%EXE%" (
  echo No %EXE% yet - run hub\build_hub.bat first.
  pause & exit /b 1
)
start "" "%EXE%" --local
