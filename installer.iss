; Inno Setup script - builds dist\CreditReportTool-Setup.exe from dist\CreditReportTool\ (see build_exe.ps1).
; Installs per-user (no administrator rights needed). Settings and data live in
; %LOCALAPPDATA%\CreditReportTool and are NOT removed on uninstall.

#define AppName "Credit Report Tool"
#define AppExe "CreditReportTool.exe"

[Setup]
AppId={{6B0E4C6E-3D1B-4E55-9C7A-0C2B8A7D1E01}
AppName={#AppName}
AppVersion=1.0.0
DefaultDirName={localappdata}\Programs\CreditReportTool
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=CreditReportTool-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "autostart"; Description: "Start the tool automatically when I sign in to Windows"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "dist\CreditReportTool\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autoprograms}\{#AppName} - Chrome for KR883 login"; Filename: "{app}\{#AppExe}"; Parameters: "--chrome"
Name: "{autoprograms}\{#AppName} - Authenticator code"; Filename: "{app}\{#AppExe}"; Parameters: "--totp"
Name: "{autoprograms}\{#AppName} - Settings"; Filename: "{app}\{#AppExe}"; Parameters: "--setup"
Name: "{autoprograms}\{#AppName} - Stop"; Filename: "{app}\{#AppExe}"; Parameters: "--stop"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--no-browser"; Tasks: autostart

[Run]
; First run: the setup window (panel logins, ngrok, optional KR883 details), then the tool starts.
Filename: "{app}\{#AppExe}"; Parameters: "--setup"; Description: "Open the setup window now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--stop --quiet"; Flags: runhidden; RunOnceId: "StopCreditReportTool"
