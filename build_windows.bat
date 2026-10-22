@echo off
REM Builds a single-file Windows executable: dist\RemoteSSHConsole.exe
REM Requires Python 3.9+ from python.org (tick "Add python.exe to PATH").
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements.txt pyinstaller || goto :error

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --add-data "assets;assets" ^
  --name RemoteSSHConsole app.py || goto :error

echo.
echo Done. Your executable is: %~dp0dist\RemoteSSHConsole.exe
pause
exit /b 0

:error
echo.
echo Build failed - see the messages above.
pause
exit /b 1
