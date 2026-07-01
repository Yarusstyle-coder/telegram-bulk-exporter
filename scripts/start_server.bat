@echo off
REM ---------------------------------------------------------------------------
REM Telegram Bulk Exporter — start helper.
REM
REM Boots the FastAPI server via uv with UTF-8 forced.
REM Does NOT kill an existing listener — if port :8765 is taken,
REM uvicorn will fail loudly and you should use restart_server.bat.
REM
REM Usage:
REM     scripts\start_server.bat            (default port 8765)
REM     scripts\start_server.bat 9000       (override port via WEB_PORT)
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "PROJECT_ROOT=%CD%"

if not "%~1"=="" set "WEB_PORT=%~1"
if not defined WEB_PORT set "WEB_PORT=8765"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo [start] http://127.0.0.1:%WEB_PORT%
if defined TDL_BINARY_PATH echo [start]   TDL_BINARY_PATH = %TDL_BINARY_PATH%
if defined DATA_DIR        echo [start]   DATA_DIR        = %DATA_DIR%
if defined EXPORT_DIR      echo [start]   EXPORT_DIR      = %EXPORT_DIR%
echo.
uv run python -m src.main
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [start] Server stopped (exit code %EXIT_CODE%).
if not "%EXIT_CODE%"=="0" (
    echo [start] Press any key to close...
    pause >nul
)

popd >nul
endlocal
