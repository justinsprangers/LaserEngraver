; Inno Setup script for Laser Engraver. Run via build.bat (it passes AppVersion).
; Installs per-user (no admin prompt) into %LOCALAPPDATA%\Programs\Laser Engraver.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1C2E7A-4B8D-4E2F-9A1C-3D5E7F9B0A42}
AppName=Laser Engraver
AppVersion={#AppVersion}
AppVerName=Laser Engraver {#AppVersion}
DefaultDirName={autopf}\Laser Engraver
DefaultGroupName=Laser Engraver
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer
OutputBaseFilename=LaserEngraver-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\LaserEngraver.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[InstallDelete]
; Clear old libraries on upgrade so nothing stale is left behind. Your settings are elsewhere.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\LaserEngraver\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Laser Engraver"; Filename: "{app}\LaserEngraver.exe"
Name: "{autodesktop}\Laser Engraver"; Filename: "{app}\LaserEngraver.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LaserEngraver.exe"; Description: "Open Laser Engraver"; Flags: postinstall nowait skipifsilent
