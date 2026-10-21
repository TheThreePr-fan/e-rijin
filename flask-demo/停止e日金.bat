@echo off
title e日金 Demo 停止
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>nul
echo 已清理 5000 端口残留，窗口 2 秒后自动关闭
ping -n 3 127.0.0.1 >nul
