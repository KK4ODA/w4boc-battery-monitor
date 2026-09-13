@echo off
REM Creates a Startup-folder shortcut so run.bat launches at login, and
REM removes the v1 shortcuts (run_monitor / run_dashboard) if present.
setlocal
set "APPDIR=%~dp0.."
for %%I in ("%APPDIR%") do set "APPDIR=%%~fI"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
del /q "%STARTUP%\run_monitor.lnk" "%STARTUP%\run_dashboard.lnk" 2>nul
del /q "%STARTUP%\run_monitor.bat - Shortcut.lnk" "%STARTUP%\run_dashboard.bat - Shortcut.lnk" 2>nul
del /q "%STARTUP%\run_monitor - Shortcut.lnk" "%STARTUP%\run_dashboard - Shortcut.lnk" 2>nul
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%STARTUP%\W4BOC Battery Monitor.lnk'); $s.TargetPath = '%APPDIR%\run.bat'; $s.WorkingDirectory = '%APPDIR%'; $s.WindowStyle = 7; $s.Description = 'W4BOC Battery Monitor'; $s.Save()"
if errorlevel 1 (
  echo FAILED to create the shortcut.
  exit /b 1
)
echo Startup shortcut created:
echo   "%STARTUP%\W4BOC Battery Monitor.lnk"  -^>  "%APPDIR%\run.bat"
endlocal
