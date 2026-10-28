@echo off
rem ---------------------------------------------------------------------------
rem  Builds Store WMS for Windows:
rem    dist\StoreWMS\StoreWMS.exe          (the program)
rem    dist\StoreWMS-Setup-1.0.1.exe       (the installer, if Inno Setup 6 is installed)
rem  Needs: Python 3.12 or 3.13 (64-bit) from python.org, "Add to PATH" ticked.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo Creating Python environment...
    py -3 -m venv .venv || python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error

echo Installing packages...
python -m pip install --upgrade pip >nul
pip install -r requirements-dev.txt || goto :error

echo Running tests...
set QT_QPA_PLATFORM=offscreen
python -m pytest -q || goto :error
set QT_QPA_PLATFORM=

echo Building program...
pyinstaller packaging\StoreWMS.spec --noconfirm --clean || goto :error

set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    echo Building installer...
    %ISCC% packaging\installer.iss || goto :error
    echo.
    echo DONE. Installer: dist\StoreWMS-Setup-1.0.1.exe
) else (
    echo.
    echo DONE. Program folder: dist\StoreWMS  ^(install Inno Setup 6 from jrsoftware.org to also get a Setup.exe^)
)
exit /b 0

:error
echo.
echo BUILD FAILED - see the messages above.
exit /b 1
