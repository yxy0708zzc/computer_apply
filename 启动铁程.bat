@echo off
chcp 65001 >nul
title 铁程 · 智能火车出行规划
cd /d "C:\vscode_py\tripai"

echo ============================================
echo   铁程 · 智能火车出行规划
echo   http://127.0.0.1:8600
echo ============================================
echo  [启动] 服务启动中，浏览器将自动打开...
echo  [停止] 关闭本窗口即停止服务
echo.

rem 2 秒后自动打开浏览器（等服务就绪）
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:8600"

"C:\vscode_py\tripai\.venv\Scripts\python.exe" -m app.server

echo.
echo [铁程] 服务已退出。
pause
