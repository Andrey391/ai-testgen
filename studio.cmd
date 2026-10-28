@echo off
rem Start the studio; arguments are passed to scripts\studio.ps1 (e.g. studio.cmd -NoAuth).
rem The .cmd works under any PowerShell execution policy, including the default Restricted one.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\studio.ps1" %*
set code=%errorlevel%
rem Opened by double-click: keep the window so the error stays readable.
if not "%code%"=="0" echo %cmdcmdline% | find /i "/c" >nul && pause
exit /b %code%
