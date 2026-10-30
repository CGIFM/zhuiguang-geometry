# 追光几何 · 安装（免管理员）
#
# 用法（在这个目录里右键「用 PowerShell 运行」，或者）：
#   powershell -ExecutionPolicy Bypass -File tools\install.ps1
#   powershell -ExecutionPolicy Bypass -File tools\install.ps1 -Port 8795 -NoAutostart
#
# 做四件事：
#   1. 建桌面/开始菜单快捷方式
#   2. 装一个 zgr 命令行入口（加进用户 PATH，不用管理员）
#   3. 开机自动起后台服务（写到 HKCU\Run，当前用户生效）
#   4. 尽量加一条防火墙放行（需要管理员，加不上就跳过 —— 只影响局域网同步）
param(
    [int]$Port = 8795,
    [switch]$NoAutostart,
    [switch]$NoFirewall
)
$ErrorActionPreference = 'Stop'

# 脚本可能在 <包>\tools\ 下，也可能被拷到包的根目录
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $root 'zgr.py'))) { $root = $PSScriptRoot }
$zgr = Join-Path $root 'zgr.py'
if (-not (Test-Path $zgr)) { throw "找不到 zgr.py，请在解压出来的目录里运行本脚本（当前：$root）" }

function Find-Python {
    $embedded = Join-Path $root 'python\python.exe'
    if (Test-Path $embedded) { return @{ Exe = $embedded; W = (Join-Path $root 'python\pythonw.exe') } }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $out = & $py.Source -3 -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $exe = $out.Trim()
            $w = Join-Path (Split-Path $exe) 'pythonw.exe'
            return @{ Exe = $exe; W = $w }
        }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $w = Join-Path (Split-Path $cmd.Source) 'pythonw.exe'
        return @{ Exe = $cmd.Source; W = $w }
    }
    throw '找不到 Python。请用「便携版」（自带 python\ 目录），或先安装 Python 3.10+。'
}

$pyInfo = Find-Python
Write-Host ("使用 Python：" + $pyInfo.Exe)

function New-Shortcut($path, $target, $workdir, $icon, $args) {
    $dir = Split-Path -Parent $path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($path)
    $sc.TargetPath = $target
    if ($args) { $sc.Arguments = $args }
    $sc.WorkingDirectory = $workdir
    if ($icon -and (Test-Path $icon)) { $sc.IconLocation = "$icon,0" }
    $sc.Description = '追光几何 · 本地版本记录 + 内网同步'
    $sc.WindowStyle = 1
    $sc.Save()
    Write-Host "快捷方式：$path"
}

$ico = Join-Path $root 'assets\zgr.ico'
$launcher = Join-Path $root '启动追光几何.bat'
$desktop = [Environment]::GetFolderPath('Desktop')
New-Shortcut (Join-Path $desktop '追光几何.lnk') $launcher $root $ico $Port
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
New-Shortcut (Join-Path $startMenu '追光几何.lnk') $launcher $root $ico $Port

# 命令行入口
$bin = Join-Path $env:LOCALAPPDATA 'ZhuiguangGeometry\bin'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
$shim = "@echo off`r`n`"$($pyInfo.Exe)`" `"$zgr`" %*`r`n"
Set-Content -Path (Join-Path $bin 'zgr.cmd') -Value $shim -Encoding ASCII
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
$parts = $userPath -split ';' | Where-Object { $_ }
if ($parts -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', (($parts + $bin) -join ';'), 'User')
    Write-Host "已把 $bin 加进用户 PATH（新开终端后可用 zgr 命令）"
} else {
    Write-Host "命令行入口已存在：$bin\zgr.cmd"
}

# 开机自启：后台静默服务（pythonw，没有黑框）
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
if ($NoAutostart) {
    Remove-ItemProperty -Path $runKey -Name 'ZhuiguangGeometry' -ErrorAction SilentlyContinue
    Write-Host '已关闭开机自启'
} elseif ($pyInfo.W -and (Test-Path $pyInfo.W)) {
    $cmd = '"' + $pyInfo.W + '" "' + $zgr + '" serve --port ' + $Port + ' --host 0.0.0.0'
    New-ItemProperty -Path $runKey -Name 'ZhuiguangGeometry' -Value $cmd -PropertyType String -Force | Out-Null
    Write-Host "已设置开机自启（端口 $Port）"
} else {
    Write-Host '没找到 pythonw.exe，跳过开机自启（可以自己把快捷方式拖进「启动」文件夹）'
}

# 防火墙（只在「专用网络」放行，给局域网同步用）
if (-not $NoFirewall) {
    $admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
             ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) {
        & netsh advfirewall firewall delete rule name="ZhuiguangGeometry-$Port" | Out-Null
        & netsh advfirewall firewall add rule name="ZhuiguangGeometry-$Port" dir=in action=allow `
            protocol=TCP localport=$Port profile=private | Out-Null
        Write-Host "已放行防火墙（专用网络，TCP $Port）"
    } else {
        Write-Host '没有管理员权限，跳过防火墙。需要别的电脑连过来时，用管理员重跑一次本脚本即可。'
    }
}

Write-Host ''
Write-Host '安装完成。双击桌面「追光几何」启动。' -ForegroundColor Green
Write-Host "网页地址：http://127.0.0.1:$Port/" -ForegroundColor Green
