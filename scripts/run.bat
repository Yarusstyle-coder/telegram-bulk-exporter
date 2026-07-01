@echo off
REM ---------------------------------------------------------------------------
REM Telegram Bulk Exporter — one-click project runner.
REM
REM What this does:
REM   1. Resolves the project root relative to this .bat (works from
REM      Explorer double-click and from any cwd).
REM   2. Kills anything currently listening on the target port so a
REM      stale instance from a previous shell can't block us.
REM   3. Forces UTF-8 (the structured logger crashes on Cyrillic
REM      without it on Windows).
REM   4. Pops a browser tab pointing at the server.
REM   5. Boots `uv run python -m src.main` in the foreground so you
REM      can read its log + Ctrl+C cleanly.
REM
REM Usage:
REM     scripts\run.bat            (port 8765)
REM     scripts\run.bat 9000       (custom port)
REM ---------------------------------------------------------------------------

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "PROJECT_ROOT=%CD%"
for %%F in ("%PROJECT_ROOT%") do set "PROJECT_NAME=%%~nxF"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

REM ---------- Kill stale listener on the port ----------
echo [run] freeing port %PORT% ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo [run]   killing PID %%P
    taskkill /F /PID %%P >nul 2>&1
)

REM ---------- Drop any orphan python.exe spawned from THIS project ----------
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq python.exe" /FO CSV /NH 2^>nul') do (
    set "PID=%%~P"
    set "PID=!PID:"=!"
    wmic process where "ProcessId=!PID!" get CommandLine /FORMAT:LIST 2>nul | findstr /C:"%PROJECT_NAME%" >nul
    if not errorlevel 1 (
        echo [run]   killing orphan python PID !PID!
        taskkill /F /PID !PID! >nul 2>&1
    )
)

REM ---------- Open the browser shortly after the server should be up ----------
start "" "" cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:%PORT%/"

REM ---------- Boot the server ----------
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "WEB_PORT=%PORT%"
echo.
echo [run] booting on http://127.0.0.1:%PORT%
if defined TDL_BINARY_PATH echo [run]   TDL_BINARY_PATH = %TDL_BINARY_PATH%
if defined DATA_DIR        echo [run]   DATA_DIR        = %DATA_DIR%
if defined EXPORT_DIR      echo [run]   EXPORT_DIR      = %EXPORT_DIR%
echo.
uv run python -m src.main

popd >nul
endlocal
