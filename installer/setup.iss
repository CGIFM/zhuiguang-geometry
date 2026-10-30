; 追光几何 · Windows 安装包（Inno Setup 6）
;
; 由 tools/build_installer.py 生成，请不要直接改这里的版本号——
; 脚本会用 ISCC /DAppVer=... 覆盖。
;
; 设计取向：
;   · 装到当前用户目录，全程不要管理员、不弹 UAC
;   · 自带 Python 运行时，目标机器不需要装任何东西
;   · 关闭程序后可以在「应用和功能」里正常卸载
;   · 卸载只删程序，不动用户的项目文件夹和 %APPDATA% 里的设置

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "payload"
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif

#define AppName "追光几何"
; 双击图标启动的是原生桌面客户端（WPF + WebView2），不是浏览器
#define AppExe "追光几何.exe"
#define Publisher "jlc"
#define AppUrl "https://github.com/"
#define AppId "{{7C1F5A2E-3B4D-4E6A-9F1C-2D8B6A4E5C31}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
AppPublisher={#Publisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}
AppUpdatesURL={#AppUrl}
DefaultDirName={localappdata}\Programs\ZhuiguangGeometry
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 只装当前用户：不需要管理员，公司电脑没权限也能装
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=追光几何-{#AppVer}-安装版
SetupIconFile=..\assets\zgr.ico
UninstallDisplayIcon={app}\assets\zgr.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 程序正在跑的时候提示先关掉，别硬覆盖
CloseApplications=yes
RestartApplications=no
; 服务在跑的时候占着 python/*.dll，安装和卸载都会写不进去。
; 服务启动时会创建这个互斥量，安装器据此提示「请先退出」。
AppMutex=ZhuiguangGeometryRunning,Global\ZhuiguangGeometryRunning
AllowNoIcons=yes
LicenseFile=LICENSE.txt

[Languages]
Name: "cn"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "开机自动启动（后台静默运行，无窗口）"; GroupDescription: "启动选项："
Name: "addtopath"; Description: "把 zgr 命令加进 PATH（命令行里可直接用）"; GroupDescription: "启动选项："

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 双击图标：打开原生桌面窗口（服务没起会自己起，没有黑窗口），全程不碰浏览器
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; \
  WorkingDir: "{app}"; IconFilename: "{app}\assets\zgr.ico"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.txt"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; \
  WorkingDir: "{app}"; IconFilename: "{app}\assets\zgr.ico"; Tasks: desktopicon

[Run]
; 装完直接启动一次（用户可以在向导里取消勾选）
Filename: "{app}\{#AppExe}"; \
  WorkingDir: "{app}"; Description: "立即启动 {#AppName}"; \
  Flags: nowait postinstall skipifsilent

[Registry]
; 开机自启：写 HKCU，不需要管理员；--silent 只起后台同步 + 托盘，不弹窗口
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "ZhuiguangGeometry"; \
  ValueData: """{app}\{#AppExe}"" --silent"; \
  Flags: uninsdeletevalue; Tasks: autostart

[UninstallDelete]
; 整个程序目录都清掉：卸载器只会删「安装时记录过」的文件，
; python\ 下的运行时和运行时生成的 __pycache__ 会剩一堆，
; 这里兜底。用户数据在 %APPDATA%\zhuiguang-record，不在这里，不会丢。
Type: filesandordirs; Name: "{app}"

[Code]
// ---------------------------------------------------------------- PATH
// 只在「加进 PATH」被勾选时改用户 PATH，卸载时再摘掉，不动系统 PATH
const
  EnvKey = 'Environment';

function ReadPath(): string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Result) then
    Result := '';
end;

procedure WritePath(const Value: string);
begin
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvKey, 'Path', Value);
end;

function PathHas(const Path, Dir: string): Boolean;
begin
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') > 0;
end;

procedure AddToPath(const Dir: string);
var
  P: string;
begin
  P := ReadPath();
  if PathHas(P, Dir) then exit;
  if (P <> '') and (P[Length(P)] <> ';') then P := P + ';';
  WritePath(P + Dir);
end;

procedure RemoveFromPath(const Dir: string);
var
  Rest, Piece, Acc: string;
  i: Integer;
begin
  Rest := ReadPath();
  if Rest = '' then exit;
  Acc := '';
  while Rest <> '' do
  begin
    i := Pos(';', Rest);
    if i = 0 then
    begin
      Piece := Rest;
      Rest := '';
    end
    else
    begin
      Piece := Copy(Rest, 1, i - 1);
      Rest := Copy(Rest, i + 1, Length(Rest));
    end;
    // 逐段拼回去，别人的路径原样保留
    if (Piece <> '') and (Uppercase(Piece) <> Uppercase(Dir)) then
    begin
      if Acc <> '' then Acc := Acc + ';';
      Acc := Acc + Piece;
    end;
  end;
  WritePath(Acc);
end;

// ---------------------------------------------------------------- 生命周期
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
