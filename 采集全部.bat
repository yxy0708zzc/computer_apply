@echo off
chcp 65001 >nul
title 铁程 · 数据采集全流程
cd /d "C:\vscode_py\tripai"
set PY=C:\vscode_py\tripai\.venv\Scripts\python.exe

echo ============================================
echo   铁程 · 数据采集全流程（车站/车次/票价）
echo   随时可 Ctrl+C 中断，重跑本脚本自动续爬
echo ============================================
echo.

echo [1/3] 车站表 ...
%PY% -m collect.stations
if errorlevel 1 goto :fail

echo.
echo [2/3] 车次 + 经停（全字头 K G C T Z D + 数字，约 40~60 分钟）...
%PY% -m collect.collector
if errorlevel 1 goto :fail

echo.
echo [3/3] 票价（全量车次，约 2~4 小时，建议 3 线程）...
%PY% -m collect.price_collector --resume --workers 3
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   [完成] 全部采集结束！
echo ============================================
%PY% -m collect.collector --stats
%PY% -m collect.price_collector --stats
echo.
echo [体检] 可选：collect.cleanup --check 检查数据完整性
pause
exit /b 0

:fail
echo.
echo [失败] 上一步骤出错。已入库数据不会丢失，
echo        修复问题后重新运行本脚本即可续爬（已完成部分自动跳过）。
pause
exit /b 1
