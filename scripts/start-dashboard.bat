@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Sentinel dashboard
pushd "%~dp0.."
if errorlevel 1 goto location_error
echo Sentinel uses Node.js and npm for its first frontend build.
echo Later starts reuse the build unless the frontend has changed.
echo.

where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto try_python
py -3 "%~dp0dashboard.py" --demo --open-browser --port 0
goto done

:try_python
where python >nul 2>&1
if errorlevel 1 goto missing_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
if errorlevel 1 goto missing_python
python "%~dp0dashboard.py" --demo --open-browser --port 0
goto done

:missing_python
echo Python 3.12 or newer is required. Install Python, then run this launcher again.
goto done

:location_error
echo Could not open the repository directory. Keep this launcher in the scripts folder.
pause
exit /b 1

:done
echo.
echo The dashboard has stopped. Any error is shown above.
popd
pause
