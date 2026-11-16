; Per-user desktop installer, with the official WebView2 bootstrapper.
[Setup]
AppId={{20A721D6-356D-43F0-80D5-98FC31D9A0E3}
AppName=Maestro
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Maestro
DefaultGroupName=Maestro
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputPath}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Maestro.exe
CloseApplications=yes

[Files]
Source: "{#BundlePath}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#WebViewSetup}"; DestDir: "{tmp}"; DestName: "WebView2Setup.exe"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Maestro"; Filename: "{app}\Maestro.exe"
Name: "{group}\Uninstall Maestro"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Maestro.exe"; Description: "Launch Maestro"; Flags: nowait postinstall skipifsilent

[Code]
function NeedsWebView2: Boolean;
var Version: String;
begin
  Result := not ((RegQueryStringValue(HKLM32,
    'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version)
    or RegQueryStringValue(HKCU,
    'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version))
    and (Version <> '') and (Version <> '0.0.0.0'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var Code: Integer;
begin
  if (CurStep = ssPostInstall) and NeedsWebView2 then begin
    if not Exec(ExpandConstant('{tmp}\WebView2Setup.exe'), '/silent /install', '',
      SW_HIDE, ewWaitUntilTerminated, Code) then
      RaiseException('Microsoft WebView2 setup could not start.');
    if NeedsWebView2 then
      RaiseException('Microsoft WebView2 is required. Check your internet connection and run the installer again.');
  end;
end;
