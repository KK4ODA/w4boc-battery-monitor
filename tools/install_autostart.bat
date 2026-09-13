@echo off
REM Enables "Start with Windows" (same as the switch on the dashboard Settings page).
setlocal
cd /d "%~dp0.."
python -m w4boc.autostart enable
endlocal
