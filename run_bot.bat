@echo off
rem Run the autopatrol timeline bot (see README.md).
rem Optional: set APBOT_PYTHON to a full interpreter path, e.g. a venv.
cd /d "%~dp0"
chcp 65001 >nul 2>nul
set PYTHONIOENCODING=utf-8
if exist "%~dp0vendor\" set PYTHONPATH=%~dp0vendor

if defined APBOT_PYTHON (
    "%APBOT_PYTHON%" ap_bot.py %*
    exit /b %errorlevel%
)
py -3 ap_bot.py %*
if errorlevel 9009 python ap_bot.py %*
