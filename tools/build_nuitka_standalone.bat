@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "%~dp0build.ps1" -Mode standalone
pause
