@echo off
setlocal EnableExtensions DisableDelayedExpansion
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-dependencies.ps1" %*
set "sentinel_install_exit=%errorlevel%"
echo.
echo Dependency check finished with exit code %sentinel_install_exit%.
if /i not "%~1"=="-CheckOnly" pause
exit /b %sentinel_install_exit%
