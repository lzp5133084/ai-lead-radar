@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
"%~dp0runtime\python.exe" -m unittest discover -s tests -v
pause
