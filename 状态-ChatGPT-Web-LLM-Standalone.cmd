@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Plugins\extensions\chatgpt-web-llm-adapter\launch-standalone.ps1" -Mode status
set "EC=%ERRORLEVEL%"
pause
exit /b %EC%
