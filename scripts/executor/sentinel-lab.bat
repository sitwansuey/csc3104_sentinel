@echo off
setlocal EnableExtensions DisableDelayedExpansion
if /i "%~1"=="--run" goto run
start "Sentinel local Kubernetes lab" "%ComSpec%" /d /k ""%~f0" --run"
exit /b

:run
title Sentinel local Kubernetes lab
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0sentinel-lab.ps1"
echo.
echo Lab menu closed. Output remains visible. Type exit to close this terminal.
exit /b
