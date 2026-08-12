@echo off
REM Start the AI Gaming Video Editor.
REM
REM Double-click this file. It finds a Python that has the dependencies, builds
REM the interface if it needs building, starts the API and the job worker, and
REM opens the browser. One thing to run.
REM
REM This file must keep CRLF line endings. cmd.exe misparses LF-only batch
REM files inside blocks, and the first version of this shipped that way: the
REM interpreter search silently found nothing and reported that no Python had
REM the dependencies, on a machine where two of them did.

cd /d "%~dp0"
title AI Gaming Video Editor

echo.
echo   AI Gaming Video Editor
echo   ----------------------
echo.

REM Find an interpreter that can actually run this. Plain `python` is whichever
REM one is first on PATH, and on a machine with several installed that is
REM rarely the one the dependencies went into. Flat gotos rather than a for
REM loop: no nested blocks, no delayed expansion, nothing to misparse.
set "VAI_PY="

py -3.11 -c "import uvicorn" >nul 2>&1
if not errorlevel 1 set "VAI_PY=py -3.11"
if defined VAI_PY goto :found

python -c "import uvicorn" >nul 2>&1
if not errorlevel 1 set "VAI_PY=python"
if defined VAI_PY goto :found

py -c "import uvicorn" >nul 2>&1
if not errorlevel 1 set "VAI_PY=py"
if defined VAI_PY goto :found

echo   No Python on this machine has the dependencies installed.
echo.
echo   Install them with:
echo       py -3.11 -m pip install -e ".[dev]"
echo.
pause
exit /b 1

:found
echo   Python      %VAI_PY%

REM Build the interface only when it is missing. A rebuild is about two
REM seconds, but doing it every launch would still be two seconds of nothing
REM appearing to happen.
if exist "apps\web\dist\index.html" goto :serve

echo   Interface   building, one moment...
call npm run build -w apps/web >nul 2>&1
if not errorlevel 1 goto :serve
echo.
echo   The interface could not be built. Run this to see why:
echo       npm run build -w apps/web
echo.
echo   Starting the API on its own instead.

:serve
echo.
REM Open the browser shortly after the server starts listening. `start` returns
REM immediately, so this does not hold up the server.
REM `ping` rather than `timeout` for the delay: `timeout` refuses to run when
REM stdin is redirected, which it is whenever this is launched from anything
REM but a console window.
start "" /b cmd /c "ping -n 5 127.0.0.1 >nul & start http://127.0.0.1:8765"

%VAI_PY% scripts\serve.py
if errorlevel 1 (
    echo.
    echo   The application stopped. The message above says why.
    pause
)
