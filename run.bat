@echo off
REM run.bat - starts the W4BOC Battery Monitor under its supervisor (launcher.py).
REM The launcher restarts the app on crashes, kills it if it wedges, and
REM applies auto-updates. Close this window (or Ctrl-C) to stop everything.
REM
REM This file is never touched by the auto-updater.

setlocal
cd /d "%~dp0"
if not exist logs mkdir logs
title W4BOC Battery Monitor

:loop
python launcher.py %*
if %errorlevel%==0 goto end
echo [%date% %time%] launcher exited with code %errorlevel% -- restarting in 15s (Ctrl-C to abort)
echo [%date% %time%] launcher exited code %errorlevel%, restarting in 15s >> logs\wrapper.log
timeout /t 15 /nobreak > nul
goto loop

:end
endlocal
