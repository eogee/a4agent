@echo off
chcp 65001 >nul
cd /d %~dp0
title a4agent 浏览器调试版
echo 正在启动 a4agent 浏览器调试版（服务就绪后会自动打开浏览器）...
echo 使用说明：调试期间请保留本窗口；停止调试直接关闭本窗口。
uv run python dev_server.py
pause
