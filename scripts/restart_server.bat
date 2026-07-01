@echo off
REM ---------------------------------------------------------------------------
REM Telegram Bulk Exporter — restart helper.
REM
REM 1) Kills any process listening on the configured port (default 8765),
REM    even if it's a stale instance from a previous shell.
REM 2) Boots the FastAPI server via uv with UTF-8 forced — the
REM    structured logger crashes on Cyrillic without these env vars
REM    on Windows.
REM 3) Pauses on exit so you can read the error if the server died.
REM
REM Usage:
REM     scripts\restart_server.bat            (uses port 8765)
REM     scripts\restart_server.bat 9000       (override port)
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

REM Resolve project root = parent of this script.
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "PROJECT_ROOT=%CD%"
for %%F in ("%PROJECT_ROOT%") do set "PROJECT_NAME=%%~nxF"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

echo [restart] Killing anything on port %PORT% ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo [restart]   killing PID %%P
    taskkill /F /PID %%P >nul 2>&1
)

REM Drop any orphaned python processes spawned out of THIS project's
REM venv. We grep CommandLine for the project folder name so unrelated
REM python.exe instances on the machine are safe.
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH 2^>nul') do (
    set "PID=%%~P"
    set "PID=!PID:"=!"
    wmic process where "ProcessId=!PID!" get CommandLine /FORMAT:LIST 2>nul | findstr /C:"%PROJECT_NAME%" >nul
    if not errorlevel 1 (
        echo [restart]   killing orphan python PID !PID!
        taskkill /F /PID !PID! >nul 2>&1
    )
)

echo [restart] Booting server on http://127.0.0.1:%PORT% ...
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "WEB_PORT=%PORT%"
if defined TDL_BINARY_PATH echo [restart]   TDL_BINARY_PATH = %TDL_BINARY_PATH%
if defined DATA_DIR        echo [restart]   DATA_DIR        = %DATA_DIR%
if defined EXPORT_DIR      echo [restart]   EXPORT_DIR      = %EXPORT_DIR%
echo.
uv run python -m src.main
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [restart] Server stopped (exit code %EXIT_CODE%).
if not "%EXIT_CODE%"=="0" (
    echo [restart] Press any key to close...
    pause >nul
)
popd >nul
endlocal
