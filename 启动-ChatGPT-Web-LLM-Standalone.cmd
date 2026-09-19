@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Plugins\extensions\chatgpt-web-llm-adapter\launch-standalone.ps1" -Mode start
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" pause
exit /b %EC%
