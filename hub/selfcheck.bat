@echo off
setlocal
rem Proves the built onedir hub is complete (pak builder + Oodle module in _internal\). Shows a message box
rem and writes %LOCALAPPDATA%\CommunityHub\logs\selfcheck.txt.
cd /d "%~dp0.."
set "EXE=dist\LightsOut\LightsOut.exe"
if not exist "%EXE%" (
  echo No %EXE% yet - run hub\build_hub.bat first.
  pause & exit /b 1
)
start "" /wait "%EXE%" --selfcheck
