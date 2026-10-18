#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=<version>
#endif

#define AppName "IBKR Dashboard"
#define AppExe "IBKRDashboard.exe"

[Setup]
AppId={{d29d995f-d960-4d2c-a0b0-13dc0edefe66}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=IBKR Dashboard contributors
DefaultDirName={localappdata}\Programs\IBKR Dashboard
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist\installers
OutputBaseFilename=IBKRDashboard-{#AppVersion}-windows-x64-setup
SetupIconFile=..\..\app\assets\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
WizardStyle=modern

[Files]
Source: "..\..\dist\IBKRDashboard\IBKRDashboard.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\IBKRDashboard\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\PortfolioMonitor.exe"
Type: files; Name: "{autoprograms}\Portfolio Monitor.lnk"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Code]
procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.NextButton.Left := WizardForm.CancelButton.Left;
  end;
end;
