@echo off
setlocal
cd /d "%~dp0"

set "PYW="
where pythonw >nul 2>&1 && set "PYW=pythonw"
if not defined PYW if exist "C:\Python314\pythonw.exe" set "PYW=C:\Python314\pythonw.exe"

if defined PYW (
    start "" "%PYW%" "xic_gui.py"
) else (
    python "xic_gui.py"
    if errorlevel 1 pause
)
