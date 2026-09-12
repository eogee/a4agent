; ============================================================================
;  a4agent 安装包模板
;  由 build.ps1 以命令行定义实例化：
;    ISCC installer.iss /DBackendName=cu13.3 /DVersion=0.1.0
;                       /DPublishDir=C:\...\publish
;                       /DEngineSource=C:\...\payloads\cuda13.3
;                       /DAppId=a4agent-cu13.3
;  Lite 模式（不内置引擎，首次运行向导在线下载）：
;    不传 /DEngineSource 即可（build.ps1 -Lite）
; ============================================================================

#ifndef BackendName
#define BackendName "cu12.4"
#endif
#ifndef Version
#define Version "0.1.0"
#endif
#ifndef EngineSource
#define EngineSource ""
#endif
#ifndef AppId
#define AppId "a4agent"
#endif

[Setup]
AppId={#AppId}
AppName=a4agent ({#BackendName})
AppVersion={#Version}
AppPublisher=ProgramMine
DefaultDirName={autopf}\a4agent-{#BackendName}
PrivilegesRequired=lowest
DefaultGroupName=a4agent ({#BackendName})
OutputDir=out
OutputBaseFilename=a4agent-{#BackendName}-setup-v{#Version}
Compression=lzma2/fast
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
CloseApplications=yes
DisableProgramGroupPage=yes
SetupIconFile=..\src\App\a4agent.ico

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; \
    GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 启动器（自包含单文件发布产物）
Source: "{#PublishDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
#if EngineSource != ""
; 引擎（llama-server.exe + 必需 DLL）；Lite 模式不打包引擎，由向导在线下载
Source: "{#EngineSource}\*"; DestDir: "{app}\engine"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
Name: "{group}\a4agent ({#BackendName})"; Filename: "{app}\a4agent.exe"
Name: "{group}\卸载 a4agent ({#BackendName})"; Filename: "{uninstallexe}"
Name: "{autodesktop}\a4agent ({#BackendName})"; Filename: "{app}\a4agent.exe"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\a4agent.exe"; Description: "立即运行 a4agent 配置向导"; \
    WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
