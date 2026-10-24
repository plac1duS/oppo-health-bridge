@echo off
rem 欢太健康桥 · 双击启动图形界面（若无反应，请在终端运行 python -m bridge gui 查看报错）
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m bridge gui
) else (
  start "" pythonw -m bridge gui
)
