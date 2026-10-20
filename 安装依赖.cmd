@echo off
setlocal
cd /d "%~dp0"

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo pip failed. Falling back to offline wheel download...
    python setup_deps.py
)
echo.
echo Done. You can now run:  python xic_from_raw.py "D:\data" --mz 58.83
pause
