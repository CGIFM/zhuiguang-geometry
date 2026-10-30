@echo off
setlocal
set "ROOT=%~dp0.."
set "PY=C:/Tools\Python313\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%ROOT%\zgr.py" %*
