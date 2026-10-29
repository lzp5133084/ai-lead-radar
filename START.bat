@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" "%~dp0app.py" %*
) else (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo 请使用包含 runtime 文件夹的完整便携版，或安装 Python 3.11 及以上。
    echo 官方下载：https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
  py -3 "%~dp0app.py" %*
)
if errorlevel 1 (
  echo 启动失败，请查看上方说明；不要重复打开多个启动窗口。
  pause
)
endlocal
