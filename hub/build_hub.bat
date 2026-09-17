@echo off
setlocal
rem Builds dist\LightsOut\LightsOut.exe (one-folder PyInstaller build) and wraps it in the installer
rem dist\LightsOut-Setup-<version>.exe (Inno Setup, hub\installer.iss). Run from anywhere; it cd's to the
rem repository root. Needs Python 3.11+ from python.org (tick "Add python.exe to PATH" in its installer) and
rem Inno Setup 6.3+ (winget install -e --id JRSoftware.InnoSetup). Everything else is fetched into a local .venv.
rem
rem Unattended use: this is run over SSH on the test box, where a stray `pause` would hang forever. Every
rem failure path goes through :die, which only pauses when HUB_NOPAUSE is NOT set - so `set HUB_NOPAUSE=1`
rem (or any CI run) exits cleanly, while a plain double-click still pauses so the window stays readable.
cd /d "%~dp0.."
where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH", then run this again.
  goto :die
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating the build environment ^(.venv^)...
  python -m venv .venv || (echo Could not create .venv & goto :die)
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
python -m pip install -r hub\requirements.txt || (echo pip install failed & goto :die)

rem Inno Setup is checked BEFORE the slow part so a missing compiler fails in seconds, not after PyInstaller.
rem winget puts it in Program Files (x86) for a machine-wide install and under %LOCALAPPDATA%\Programs for a
rem per-user one; the (x86) in the path is why this uses goto labels instead of nested if-blocks (cmd
rem mis-parses a ")" inside parentheses).
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto iscc_ok
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" goto iscc_ok
echo Inno Setup 6.3+ was not found ^(looked in "%ProgramFiles(x86)%\Inno Setup 6" and "%LOCALAPPDATA%\Programs\Inno Setup 6"^).
echo Install it with:   winget install -e --id JRSoftware.InnoSetup
goto :die
:iscc_ok

rem Both versions come from hub\version.py so the installer name, the exe's version resource and the
rem installer's VersionInfoVersion cannot drift. HUB_VERSION is the display string ("1.1.0"); HUB_FILE_VERSION
rem is the numeric a.b.c.d Inno's VersionInfoVersion needs. usebackq/backticks because the Python one-liner
rem itself contains single quotes, which would otherwise end a '...' for-command.
set "HUB_VERSION="
set "HUB_FILE_VERSION="
for /f "usebackq delims=" %%v in (`python -c "import sys; sys.path.insert(0,'hub'); import version; print(version.HUB_VERSION)"`) do set "HUB_VERSION=%%v"
for /f "usebackq delims=" %%v in (`python -c "import sys; sys.path.insert(0,'hub'); import version; print(version.HUB_FILE_VERSION)"`) do set "HUB_FILE_VERSION=%%v"
if "%HUB_VERSION%"=="" (echo Could not read HUB_VERSION from hub\version.py & goto :die)
if "%HUB_FILE_VERSION%"=="" (echo Could not read HUB_FILE_VERSION from hub\version.py & goto :die)
echo Building Lights Out %HUB_VERSION% ^(file version %HUB_FILE_VERSION%^)

echo Running the hub tests...
python tests\test_hub.py || (echo TESTS FAILED - not building & goto :die)

echo Building the exe...
rem A running build cannot be overwritten or deleted (WinError 5). Preserve running hubs and
rem remove the old build folder. If the folder is locked RENAME
rem it out of the way as a fallback - Windows lets you rename a folder even while a delete on it is failing -
rem so PyInstaller always gets a free dist\LightsOut. Old installers are just deleted; nothing holds them open.
if not exist dist mkdir dist
if exist "dist\LightsOut" rd /s /q "dist\LightsOut" >nul 2>&1
if exist "dist\LightsOut" ren "dist\LightsOut" "LightsOut.old.%RANDOM%"
if exist "dist\LightsOut" (echo dist\LightsOut is locked and could not be renamed - close the hub and try again & goto :die)
del /f /q "dist\LightsOut-Setup-*.exe" >nul 2>&1
pyinstaller --noconfirm --clean hub\hub.spec > hub\build.log 2>&1 || (echo PyInstaller failed - see hub\build.log & goto :die)
if not exist "dist\LightsOut\LightsOut.exe" (echo PyInstaller finished but dist\LightsOut\LightsOut.exe is missing - see hub\build.log & goto :die)
for /d %%d in ("dist\LightsOut.old.*") do rd /s /q "%%d" >nul 2>&1

echo Building the installer...
rem /Qp = quiet, progress only; the full compiler output goes to hub\build.log after PyInstaller's. Both
rem versions are passed in as preprocessor defines so installer.iss never has to be edited for a release.
"%ISCC%" /Qp /DHubVersion=%HUB_VERSION% /DHubFileVersion=%HUB_FILE_VERSION% hub\installer.iss >> hub\build.log 2>&1 || (echo Inno Setup failed - see hub\build.log & goto :die)
if not exist "dist\LightsOut-Setup-%HUB_VERSION%.exe" (echo Inno Setup finished but dist\LightsOut-Setup-%HUB_VERSION%.exe is missing - see hub\build.log & goto :die)

echo.
echo BUILD OK
echo   dist\LightsOut\LightsOut.exe
echo   dist\LightsOut-Setup-%HUB_VERSION%.exe
dir /b dist\LightsOut\LightsOut.exe dist\LightsOut-Setup-*.exe
if not defined HUB_NOPAUSE pause
exit /b 0

:die
rem Set HUB_NOPAUSE=1 (SSH / CI) to skip the pause; a double-click leaves the window up to read the error.
if not defined HUB_NOPAUSE pause
exit /b 1
