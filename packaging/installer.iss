; Inno Setup script for Store WMS. Build with build.bat, or open in Inno Setup 6 and press Compile.
; Creates dist\StoreWMS-Setup-<version>.exe

#define AppName "Store WMS"
#define AppVersion "1.33.0"
#define AppExe "StoreWMS.exe"

[Setup]
AppId={{6A1E2C64-3F0B-4C8E-9C5D-57D0B3E1A9F2}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\StoreWMS
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=StoreWMS-Setup-{#AppVersion}
SetupIconFile=..\wms\resources\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
; Data (database, photos, scans) lives in %LOCALAPPDATA%\StoreWMS and backups in Documents\StoreWMS Backups.
; Neither is touched by installing, upgrading or uninstalling.
; Automatic updates (Settings > About, or the window at start-up) run this installer with
;   /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RELAUNCH=1
; over the existing installation (same AppId = upgrade in place, same folder, same install mode).
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\StoreWMS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent
; Automatic update: start the new version straight after a silent install.
Filename: "{app}\{#AppExe}"; Flags: nowait runasoriginaluser; Check: RelaunchAfterUpdate

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;
