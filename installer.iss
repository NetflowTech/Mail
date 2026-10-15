; Inno Setup 6 installer script for AMail
; Requires: Inno Setup 6 (jrsoftware.org/isinfo.php) — free
; Build: run build_exe.bat first, then compile this with ISCC.exe
;
; Fully offline — no download URLs, no update endpoints, no telemetry.

#define AppName    "AMail"
#define AppVersion "2.0.0"
#define AppExe     "AMail.exe"
#define SourceDir  "dist\AMail"

[Setup]
; ── Identity ─────────────────────────────────────────────────────────────────
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=AMail
; All URL fields intentionally blank — no phoning home from the installer
AppPublisherURL=
AppSupportURL=
AppUpdatesURL=

; ── Install location ─────────────────────────────────────────────────────────
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes

; ── Output ───────────────────────────────────────────────────────────────────
OutputDir=installer_output
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=assets\icon.ico

; ── Compression (LZMA2 — best ratio, fully local, no network) ────────────────
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; ── Security / elevation ─────────────────────────────────────────────────────
; Installs to Program Files — requires admin (UAC prompt on launch)
PrivilegesRequired=admin
; Close the running app before upgrading so files aren't locked
CloseApplications=yes
CloseApplicationsFilter=*{#AppExe}
RestartApplications=no

; ── Wizard appearance ────────────────────────────────────────────────────────
WizardStyle=modern
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
DisableFinishedPage=no

; ── Uninstall ────────────────────────────────────────────────────────────────
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
; Keep user data (mail database, settings) on uninstall — don't wipe their mail
CreateUninstallRegKey=yes


[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"


[Files]
; All PyInstaller output — recursive, preserves subdirectory structure
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs


[Icons]
; Start Menu
Name: "{group}\{#AppName}";       Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
; Desktop shortcut
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"


[Run]
; Offer to launch after install (checkbox, not forced)
Filename: "{app}\{#AppExe}"; \
  Description: "Launch {#AppName}"; \
  Flags: nowait postinstall skipifsilent


[UninstallRun]
; Make sure the app isn't running when the user uninstalls
Filename: "taskkill.exe"; Parameters: "/f /im {#AppExe}"; Flags: runhidden skipifdoesntexist; RunOnceId: "KillApp"


[Registry]
; No registry entries written by the installer itself.
; The app writes its own settings under HKCU\Software\AMail at runtime
; (QSettings) — those stay after uninstall so a reinstall keeps the user's prefs.
; If you want a clean wipe on uninstall, uncomment the line below:
; Root: HKCU; Subkey: "Software\AMail"; Flags: uninsdeletekey; Tasks: purgedata


[Code]
// Refuse to install if a newer version is already present.
// Prevents accidental downgrades; the uninstaller still works normally.
function InitializeSetup(): Boolean;
var
  installedVer: String;
  installedTuple, newTuple: array of Integer;
  i: Integer;
  parts: TStringList;
begin
  Result := True;
  if not RegQueryStringValue(HKLM,
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppName}_is1',
    'DisplayVersion', installedVer) then
    Exit; // not installed yet — always allow

  // Simple dotted-version comparison
  parts := TStringList.Create;
  try
    parts.Delimiter := '.';
    parts.DelimitedText := installedVer;
    SetArrayLength(installedTuple, parts.Count);
    for i := 0 to parts.Count - 1 do
      installedTuple[i] := StrToIntDef(parts[i], 0);
  finally
    parts.Free;
  end;

  parts := TStringList.Create;
  try
    parts.Delimiter := '.';
    parts.DelimitedText := '{#AppVersion}';
    SetArrayLength(newTuple, parts.Count);
    for i := 0 to parts.Count - 1 do
      newTuple[i] := StrToIntDef(parts[i], 0);
  finally
    parts.Free;
  end;

  for i := 0 to Min(High(installedTuple), High(newTuple)) do
  begin
    if newTuple[i] > installedTuple[i] then Exit; // upgrade — allow
    if newTuple[i] < installedTuple[i] then
    begin
      MsgBox('Version ' + installedVer + ' is already installed.' + #13#10 +
             'Uninstall it first before installing an older version.',
             mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;
