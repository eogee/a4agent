; a4agent 每用户安装脚本（Inno Setup 6）
; 由 build.py --installer 编译，版本号通过 /DMyAppVersion 注入。
; 改名说明（v0.4.0 起 a4api → a4agent）：
;   - AppId 保持不变：Inno 视其为同一应用的升级，卸载入口只有一份；
;   - UsePreviousAppDir=no：安装目录从 Programs\a4api 迁到 Programs\a4agent，
;     旧目录与旧快捷方式在 ssPostInstall 清理（数据目录 %APPDATA% 由应用首启自动迁移）。
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

#define MyAppName "a4agent"
#define MyAppExeName "a4agent.exe"

[Setup]
AppId={{8A3C5F71-2B4D-4E6F-9A0B-1C2D3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=a4agent
DefaultDirName={localappdata}\Programs\{#MyAppName}
UsePreviousAppDir=no
; 默认 yes 时升级安装会沿用上一次的开始菜单组名（改名用户会残留 a4api 组），强制用新组名
UsePreviousGroup=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=a4agent-setup-{#MyAppVersion}
SetupIconFile=resources\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; 主程序与后台代理（--proxy）的检测与刹停由 [Code] 的 InitializeSetup/InitializeUninstall
; 完成（AppMutex 只能覆盖持互斥体的主程序，且只能让用户手动关闭，无法一键刹停）
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
; 安装向导默认英文（Default.isl 随 Inno 自带，保证构建可复现）。
; 如需中文向导，把社区翻译文件 ChineseSimplified.isl 放到 resources\inno\ 下
; （Inno 6 不自带该文件），此条件引用会自动生效。
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists("resources\inno\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "resources\inno\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "dist\a4agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 后台翻译代理（--proxy 进程）不持 AppMutex，卸载前必须显式停掉并清理 proxy.json
Filename: "{app}\{#MyAppExeName}"; Parameters: "--proxy-stop"; Flags: runhidden

[UninstallDelete]
; 卸载后移除 {app} 空目录（unins000.exe 自删除时序可能导致目录移除被跳过）
Type: dirifempty; Name: "{app}"

[Code]
const
  AppImageName = 'a4agent.exe';
  // 改名过渡期：从 v0.3.x 升级时旧可执行文件仍是 a4api.exe，检测/刹停一并覆盖
  LegacyImageName = 'a4api.exe';
  // 同一 AppId：v0.3.x 及更早版本的卸载注册表键，InstallLocation 记录旧安装目录
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8A3C5F71-2B4D-4E6F-9A0B-1C2D3E4F5A6B}_is1';

var
  PreviousDir: String;

// 与 {app} 同目录判定：Inno 写入卸载注册表的 InstallLocation 带尾部反斜杠
// （如 '...\Programs\a4agent\'），而 {app} 展开后没有，直接字符串比较永不相等，
// 会把「同目录升级」误判为「改名迁移」，ssPostInstall 的 DelTree 将删光刚装好的
// 新版（v0.4.1 实际事故）。比较前统一去掉尾部反斜杠。
function IsDifferentDir(const A, B: String): Boolean;
begin
  Result := (A <> '') and
    (CompareText(RemoveBackslashUnlessRoot(A), RemoveBackslashUnlessRoot(B)) <> 0);
end;

// 是否有指定镜像名的进程在运行（主程序与后台代理共用同一可执行文件）
function ImageRunning(const Image: String): Boolean;
var
  Rc: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{cmd}'),
          '/C tasklist /FI "IMAGENAME eq ' + Image + '" 2>NUL | find /I "' + Image + '" >NUL',
          '', SW_HIDE, ewWaitUntilTerminated, Rc) then
    Result := (Rc = 0);
end;

function AppRunning(): Boolean;
begin
  Result := ImageRunning(AppImageName) or ImageRunning(LegacyImageName);
end;

// 刹停全部相关服务：先优雅终止（给 SQLite 落盘留 3 秒），仍存活则强制结束，
// 之后最多再等 10 秒确认退出（优雅关闭可能因应用内未完成的网络请求而拖慢进程退出）。
function StopAllAppProcesses(): Boolean;
var
  Rc: Integer;
  I: Integer;
begin
  Result := True;
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM ' + AppImageName + ' >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM ' + LegacyImageName + ' >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Rc);
  for I := 1 to 6 do
  begin
    if not AppRunning() then Exit;
    Sleep(500);
  end;
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM ' + AppImageName + ' >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM ' + LegacyImageName + ' >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, Rc);
  for I := 1 to 20 do
  begin
    if not AppRunning() then Exit;
    Sleep(500);
  end;
  Result := False;
end;

// 静默安装/卸载（如应用内更新触发）没有界面可点：AppRunning 时默认确认刹停；
// 交互安装则弹中文确认框，用户选「否」直接中止。
function ConfirmStopAndStop(): Boolean;
begin
  Result := True;
  if WizardSilent() then
    Exit;  // 静默：无需确认，交给下方 StopAllAppProcesses
  if MsgBox('检测到 a4agent（或改名前的 a4api）正在运行（主界面或后台代理服务）。' #13#10 #13#10
            '是否关闭全部相关服务并继续？',
            mbConfirmation, MB_YESNO) = IDYES then
    Exit;
  Result := False;  // 用户选择不关闭 → 中止
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  // 读取旧版本（同名 AppId）的安装目录，供 ssInstall 停旧代理、ssPostInstall 清理旧目录
  PreviousDir := '';
  RegQueryStringValue(HKCU, LegacyUninstallKey, 'InstallLocation', PreviousDir);
  if AppRunning() then
  begin
    if ConfirmStopAndStop() then
    begin
      if not StopAllAppProcesses() then
      begin
        if not WizardSilent() then
          MsgBox('无法结束 a4agent 进程，请手动关闭后重试安装。', mbCriticalError, MB_OK);
        Result := False;
      end;
    end
    else
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if AppRunning() then
  begin
    if ConfirmStopAndStop() then
    begin
      if not StopAllAppProcesses() then
      begin
        if not WizardSilent() then
          MsgBox('无法结束 a4agent 进程，请手动关闭后重试卸载。', mbCriticalError, MB_OK);
        Result := False;
      end;
    end
    else
      Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
  ExePath: String;
begin
  if CurStep = ssInstall then
  begin
    // 升级/覆盖安装：先停旧代理、清掉旧 onedir 残留（_internal 中被删的 DLL 不会自动移除，
    // 且正在运行的代理会锁住 exe/DLL 导致拷贝失败），确保全新文件落盘。
    ExePath := ExpandConstant('{app}\') + AppImageName;
    if FileExists(ExePath) then
    begin
      Exec(ExePath, '--proxy-stop', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
      DelTree(ExpandConstant('{app}\_internal'), True, True, True);
    end;
    // 从 v0.3.x 升级：旧目录（Programs\a4api）里的旧 exe 也要优雅停掉后台代理
    if IsDifferentDir(PreviousDir, ExpandConstant('{app}')) then
    begin
      ExePath := PreviousDir + '\' + LegacyImageName;
      if FileExists(ExePath) then
        Exec(ExePath, '--proxy-stop', '', SW_HIDE, ewWaitUntilTerminated, ErrorCode);
    end;
  end;

  if CurStep = ssPostInstall then
  begin
    // 改名迁移收尾：卸载注册表已指向新目录，旧安装目录与旧快捷方式一并清理
    if IsDifferentDir(PreviousDir, ExpandConstant('{app}')) and DirExists(PreviousDir) then
      DelTree(PreviousDir, True, True, True);
    DeleteFile(ExpandConstant('{userprograms}\a4api.lnk'));
    DeleteFile(ExpandConstant('{userdesktop}\a4api.lnk'));
    DeleteFile(ExpandConstant('{commonprograms}\a4api.lnk'));
    DeleteFile(ExpandConstant('{commondesktop}\a4api.lnk'));
    // 旧组文件夹（UsePreviousGroup=yes 时代 0.4.0/0.4.1 把 a4agent.lnk 装进了 a4api 组）
    if DirExists(ExpandConstant('{userprograms}\a4api')) then
      DelTree(ExpandConstant('{userprograms}\a4api'), True, True, True);
  end;
end;
