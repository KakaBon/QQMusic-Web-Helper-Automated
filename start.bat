@echo off
setlocal
cd /d "%~dp0"

py -c "import playwright" >nul 2>nul
if errorlevel 1 (
    echo Playwright is not installed in the Python used by py.
    echo.
    echo Run:
    echo     py -m pip install playwright
    echo.
    pause
    exit /b 1
)

py task-controller.py

if errorlevel 1 (
    echo.
    echo The helper exited with an error.
    pause
)

endlocal