@echo off
setlocal
title ChatGPT Web LLM Adapter - Validation
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Plugins\extensions\chatgpt-web-llm-adapter\launch-standalone.ps1" -Mode probe
set "EC=%ERRORLEVEL%"
echo.
echo ============================================================
echo Validation finished. Exit code: %EC%
echo This window will stay open. Close it manually when finished.
echo ============================================================
cmd.exe /k
