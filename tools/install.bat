@echo off
chcp 65001 >nul
title 追光几何 · 安装
setlocal
set "ROOT=%~dp0"
echo.
echo   正在安装追光几何（只写当前用户，不需要管理员）…
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\install.ps1"
echo.
pause
