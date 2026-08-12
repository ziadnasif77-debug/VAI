@echo off
REM Start the AI Gaming Video Editor. Double-click this file.
REM
REM Deliberately almost empty. Three launcher bugs in a row lived in batch
REM logic -- LF line endings cmd.exe skips silently, `timeout` refusing
REM redirected stdin, buffered output hiding the address -- so the logic now
REM lives in scripts\launch.py, where it can be tested. This file only finds
REM *a* Python (any Python: the launcher needs nothing installed) and hands
REM over. Keep it CRLF; .gitattributes enforces that.

cd /d "%~dp0"
title AI Gaming Video Editor

where py >nul 2>&1
if errorlevel 1 goto try_python

py scripts\launch.py %*
goto finished

:try_python
python scripts\launch.py %*

:finished
if errorlevel 1 pause
