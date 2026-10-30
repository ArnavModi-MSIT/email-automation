@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if not errorlevel 1 (
    python app.py
    goto finished
)

where py >nul 2>&1
if not errorlevel 1 (
    py -3 app.py
    goto finished
)

echo Python was not found. Install Python 3.10 or newer, then run this file again.
pause
exit /b 1

:finished
if errorlevel 1 (
    echo.
    echo The app exited with an error. See the message above.
    pause
)
endlocal
