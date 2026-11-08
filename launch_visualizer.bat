@echo off
REM Double-click this file to launch the interactive GMS visualizer.
REM It runs "python visualize.py --serve" and opens it in your browser.

cd /d "%~dp0"
echo Starting Gate Management System visualizer...
echo (Close this window, or press Ctrl+C, to stop the server.)
echo.

python visualize.py --serve

echo.
echo Server stopped.
pause
