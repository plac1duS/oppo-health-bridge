@echo off
rem 欢太健康桥 · 双击启动图形界面
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m bridge gui
) else (
  start "" pythonw -m bridge gui
)
