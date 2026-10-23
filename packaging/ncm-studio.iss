; NCM Studio 的 Windows 安装包（Inno Setup 6）。
;
; 构建（CI 与本地相同）：
;   iscc /DAppVersion=3.0.12 packaging\ncm-studio.iss
;
; 产物：dist\ncm-studio-windows-x86_64-setup.exe
;
; 两处设计直接关系到「自动更新能不能用」：
;
; * 安装到 %LOCALAPPDATA%\Programs（PrivilegesRequired=lowest）。程序内的更新器
;   会用下载好的新 exe 覆盖正在运行的这个文件，装到 Program Files 需要管理员
;   权限，普通用户点「更新」只会静默失败。这里刻意不提供提权选项。
; * 只装一个单文件 exe。ncmctl、mpv、Qt 都已经在它里面，安装包不再拆包，
;   升级时替换的也就是这一个文件。
;
; 应用 ID 固定不变：换掉它，新版本会被当成另一个程序，卸载列表里出现两份。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\dist\ncm-studio-windows-x86_64.exe"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "NCM Studio"
#define AppExeName "NCM Studio.exe"
#define AppPublisher "iamlinxuhan"
#define AppURL "https://github.com/iamlinxuhan/NCM-Studio"

[Setup]
AppId={{1FE06F73-7D8E-4B73-824E-399FE448D778}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} 安装程序
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

OutputDir={#OutputDir}
OutputBaseFilename=ncm-studio-windows-x86_64-setup
SetupIconFile=ncm-studio.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowNoIcons=yes

[Languages]
; 语言文件随仓库提供，不依赖 Inno Setup 安装目录里有没有这份翻译。
; 来源：Inno Setup 官方源码仓库 Files/Languages/ChineseSimplified.isl（6.5.0+）。
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "{#AppExeName}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
