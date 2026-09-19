@echo off
setlocal
echo ==============================================
echo PRAGON - LiveKit + Moss Voice Agent
echo ==============================================
echo.

set PYTHONIOENCODING=utf-8

:: Check for spidy conda environment python
set PYTHON_EXE=C:\Users\skart\anaconda3\envs\spidy\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python
)

echo Using Python: %PYTHON_EXE%
echo Reading credentials from api/api_keys.json...
echo.

"%PYTHON_EXE%" start_livekit_moss.py

echo.
echo Worker stopped.
pause

