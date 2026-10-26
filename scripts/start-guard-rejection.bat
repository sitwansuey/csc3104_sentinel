@echo off
setlocal EnableExtensions DisableDelayedExpansion
start "Sentinel Guard rejection demo" powershell.exe -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File "%~dp0test-boutique-live.ps1" -Setup -Dashboard -DashboardPort 8767 -GuardRejection -PauseBeforeInjection -Cleanup
exit /b
