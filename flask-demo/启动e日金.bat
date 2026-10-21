@echo off
title e日金 Demo 母版版
cd /d %~dp0
rem 清理 5000 端口残留进程
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>nul
rem 延迟 3 秒后打开浏览器（等服务先起来）
start "" cmd /c "ping -n 3 127.0.0.1 >nul & start http://127.0.0.1:5000"
python app.py
pause
