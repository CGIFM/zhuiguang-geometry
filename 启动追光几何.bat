@echo off
chcp 65001 >nul
title 追光几何
setlocal
set "ROOT=%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8795"

rem 优先用原生桌面客户端：独立窗口，不经过浏览器、没有黑窗口
if exist "%ROOT%追光几何.exe" (
  start "" "%ROOT%追光几何.exe" --port %PORT%
  exit /b 0
)
if exist "%ROOT%desktop\追光几何.exe" (
  start "" "%ROOT%desktop\追光几何.exe" --port %PORT%
  exit /b 0
)

rem 没有桌面客户端（纯源码跑）才退回服务 + 浏览器
set "PY="
if exist "%ROOT%python\python.exe" set "PY=%ROOT%python\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo   没有找到桌面客户端 追光几何.exe，也没有找到 Python 3.10+。
  echo   · 想直接双击用：用「追光几何-安装版.exe」或「便携版」压缩包
  echo   · 想自己装 Python：https://www.python.org/downloads/windows/
  echo.
  pause
  exit /b 1
)

echo.
echo   没有桌面客户端，改用源码模式：服务 + 浏览器
echo   ---------------------------------
echo   端口 %PORT%，关闭本窗口即停止服务。
echo.

%PY% "%ROOT%zgr.py" serve --port %PORT% --open
echo.
echo 服务已退出。
pause
