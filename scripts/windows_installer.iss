; The build script supplies all constants from audited, frozen metadata.
#if Ver != 0x06070300
  #error Use pinned Inno Setup 6.7.3
#endif
#ifndef Channel
  #error Channel must be supplied by build_windows.py
#endif
#if Channel == "stable"
  #define ProductName "Portfolio Monitor"
  #define InstallFolder "CustomIBMonitor-Stable"
  #define OtherExeName "PortfolioMonitorDev"
#elif Channel == "dev"
  #define ProductName "Portfolio Monitor Dev"
  #define InstallFolder "CustomIBMonitor-Dev"
  #define OtherExeName "PortfolioMonitor"
#else
  #error Unsupported channel
#endif

[Setup]
AppId=CustomIBMonitor.PortfolioMonitor.{#Channel}
AppName={#ProductName}
AppVersion={#DisplayVersion}
VersionInfoVersion={#NumericVersion}
AppVerName={#ProductName} {#DisplayVersion}
DefaultDirName={localappdata}\Programs\{#InstallFolder}
DefaultGroupName={#ProductName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
DisableProgramGroupPage=yes
AllowNoIcons=yes
SetupIconFile={#BundleDir}\_internal\app\assets\doge.ico
UninstallDisplayIcon={app}\{#ExeName}.exe
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
AppMutex=CustomIBMonitor.PortfolioMonitor.{#Channel}
SetupMutex=CustomIBMonitor.PortfolioMonitor.{#Channel}.Setup
; Data is outside {app}. No data/credential removal and no broad cleanup rules.

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Icons]
Name: "{group}\{#ProductName}"; Filename: "{app}\{#ExeName}.exe"; AppUserModelID: "CustomIBMonitor.PortfolioMonitor.{#Channel}"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\{#ExeName}.exe"; Tasks: desktopicon; AppUserModelID: "CustomIBMonitor.PortfolioMonitor.{#Channel}"

[Run]
Filename: "{app}\{#ExeName}.exe"; Parameters: "--postinstall-wait"; Description: "Open {#ProductName}"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\{#OtherExeName}.exe')) then
    Result := 'This folder contains the other app channel. Choose a separate installation folder.';
end;

function InitializeSetup(): Boolean;
var
  InstalledDirectory: String;
  InstalledVersion, NewVersion: Int64;
  AdmissionWait: Integer;
begin
  Result := True;
  if CheckForMutexes('CustomIBMonitor.PortfolioMonitor.{#Channel}.SetupStarting,CustomIBMonitor.PortfolioMonitor.{#Channel}.Setup') then
  begin
    MsgBox('Another Setup is already running. Finish or cancel it first.', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  if RegQueryStringValue(HKCU,
     'Software\Microsoft\Windows\CurrentVersion\Uninstall\CustomIBMonitor.PortfolioMonitor.{#Channel}_is1',
     'InstallLocation', InstalledDirectory) then
  begin
    if GetPackedVersion(InstalledDirectory + '\{#ExeName}.exe', InstalledVersion) and
       StrToVersion('{#NumericVersion}', NewVersion) then
    begin
      if ComparePackedVersion(InstalledVersion, NewVersion) > 0 then
      begin
        MsgBox('A newer version is installed. Downgrading may be incompatible with your saved data.', mbError, MB_OK);
        Result := False;
      end;
    end;
  end;
  if Result then
  begin
    { InitializeSetup runs before Inno's AppMutex check. Signal readiness now
      so the updater can close its database and exit without a circular wait.
      CreateMutex keeps this guard alive until the Setup process exits. }
    CreateMutex('CustomIBMonitor.PortfolioMonitor.{#Channel}.SetupStarting');
    if CheckForMutexes('CustomIBMonitor.PortfolioMonitor.{#Channel}.Handoff') then
      for AdmissionWait := 1 to 100 do
      begin
        if not CheckForMutexes('CustomIBMonitor.PortfolioMonitor.{#Channel}') then
          Break;
        Sleep(100);
      end;
    { Other app instances still use Inno's ordinary AppMutex prompt. }
  end;
end;
