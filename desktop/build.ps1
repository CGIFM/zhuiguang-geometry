# 编译桌面客户端：把网页装进一个真正的 Windows 窗口（WPF + WebView2，无控制台）。
# 产物：dist\desktop\追光几何.exe + 3 个 WebView2 组件 + assets\zgr.ico
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'dist\desktop'
$fw = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'
$csc = Join-Path $fw 'csc.exe'
$wpf = Join-Path $fw 'WPF'
$vendor = Join-Path $PSScriptRoot 'vendor'

foreach ($p in @($csc, (Join-Path $wpf 'PresentationFramework.dll'),
                 (Join-Path $vendor 'Microsoft.Web.WebView2.Core.dll'))) {
  if (-not (Test-Path $p)) { throw "缺少编译依赖：$p" }
}

if (-not (Test-Path $out)) { New-Item -ItemType Directory -Force $out | Out-Null }

$refs = @(
  (Join-Path $wpf 'PresentationFramework.dll'),
  (Join-Path $wpf 'PresentationCore.dll'),
  (Join-Path $wpf 'WindowsBase.dll'),
  (Join-Path $fw 'System.Xaml.dll'),
  (Join-Path $fw 'System.dll'),
  (Join-Path $fw 'System.Core.dll'),
  (Join-Path $fw 'System.Drawing.dll'),
  (Join-Path $fw 'System.Windows.Forms.dll'),
  (Join-Path $vendor 'Microsoft.Web.WebView2.Core.dll'),
  (Join-Path $vendor 'Microsoft.Web.WebView2.Wpf.dll')
) | ForEach-Object { '/r:' + $_ }

& $csc /nologo /target:winexe /platform:x64 /optimize+ /utf8output `
  ('/out:' + (Join-Path $out '追光几何.exe')) `
  ('/win32icon:' + (Join-Path $root 'assets\zgr.ico')) `
  $refs (Join-Path $PSScriptRoot 'ZhuiguangGeometry.cs')
if ($LASTEXITCODE -ne 0) { throw "csc 编译失败（$LASTEXITCODE）" }

Copy-Item (Join-Path $vendor 'Microsoft.Web.WebView2.Core.dll') $out -Force
Copy-Item (Join-Path $vendor 'Microsoft.Web.WebView2.Wpf.dll') $out -Force
Copy-Item (Join-Path $vendor 'WebView2Loader.dll') $out -Force
New-Item -ItemType Directory -Force (Join-Path $out 'assets') | Out-Null
Copy-Item (Join-Path $root 'assets\zgr.ico') (Join-Path $out 'assets') -Force

Get-ChildItem $out -Recurse -File | ForEach-Object {
  'OK ' + $_.FullName.Substring($out.Length + 1) + ' ' + $_.Length
}
