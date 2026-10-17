#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=<version>
#endif

#define AppName "Portfolio Monitor"
#define AppExe "PortfolioMonitor.exe"

[Setup]
AppId={{d29d995f-d960-4d2c-a0b0-13dc0edefe66}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Portfolio Monitor contributors
DefaultDirName={localappdata}\Programs\Portfolio Monitor
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist\installers
OutputBaseFilename=PortfolioMonitor-{#AppVersion}-windows-x64-setup
SetupIconFile=..\..\app\assets\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
LicenseFile=..\..\LICENSE
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
WizardStyle=modern

[Files]
Source: "..\..\dist\PortfolioMonitor\PortfolioMonitor.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\PortfolioMonitor\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

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
