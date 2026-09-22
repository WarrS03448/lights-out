; Inno Setup script for Lights Out. Needs Inno Setup 6.3+ (x64compatible + MinVersion below).
; Compiled by hub\build_hub.bat after PyInstaller:
;     ISCC.exe /Qp /DHubVersion=1.1.0 /DHubFileVersion=1.1.0.0 hub\installer.iss  ->  dist\LightsOut-Setup-1.1.0.exe
; Paths in here are relative to this file (hub\), so the PyInstaller output is ..\dist\LightsOut.
;
; A standard installer manages the application folder, shortcuts, updates and removal.
; The official release installer is digitally signed after packaging; see BUILDING.md.
;
; Decisions
;   * Per-user install, no UAC (PrivilegesRequired=lowest). The hub updates itself by running this
;     installer silently, and a silent install must never need elevation.
;   * The program folder is <Downloads>\LightsOut (was <Downloads>\CommunityHub before the Lights Out
;     rename, and {localappdata}\CommunityHub through 2.0.20). UsePreviousAppDir is on (Inno's default), so
;     A tester who ran the setup read "AppData\Local" off the destination page and reported that the
;     download had gone somewhere hidden; Downloads is the folder they expect a thing they downloaded to
;     be in. UsePreviousAppDir is on (Inno's default), so an existing install keeps the folder it already
;     has and only a FRESH install lands in Downloads - no orphaned copy, and the silent update path is
;     unchanged. Note this also finally separates the program folder from the state folder, which happened
;     to be the same directory before.
;   * CloseApplications=force: a hub that is still holding files gets closed rather than the install
;     stopping to ask. RestartApplications=no because /LAUNCHHUB=1 (below) decides whether it comes back.
;   * No AppMutex. With /SUPPRESSMSGBOXES a mutex the old hub still holds would abort the silent update
;     instead of prompting; CloseApplications already handles the running-hub case.
;   * Uninstall removes only {app} (the program folder). %LOCALAPPDATA%\CommunityHub (state.json, packs,
;     sign-in) is a different folder and is left in place on purpose so a reinstall picks up where the
;     user left off.
;   * Silent update command line the updater (hub\update.py) uses:
;       /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /LAUNCHHUB=1 /LOG=<path>
;     /LAUNCHHUB=1 is our own parameter: the [Run] entry with Check: LaunchHub starts the installed hub
;     after a silent install (the normal postinstall entry is skipifsilent, so it never runs twice).

; HubVersion (display string, e.g. "1.1.0" or "1.2.0-beta") and HubFileVersion (numeric a.b.c.d for
; VersionInfoVersion, which rejects a pre-release suffix) normally arrive from the command line
; (/DHubVersion=... /DHubFileVersion=...). The fallbacks keep the script compilable from the Inno IDE
; for a quick test; a release build (build_hub.bat) always passes both explicitly.
#ifndef HubVersion
  #define HubVersion "0.0.0"
#endif
#ifndef HubFileVersion
  #define HubFileVersion "0.0.0.0"
#endif
#define AppName "Lights Out"

[Setup]
; AppId ties installs, upgrades and the uninstall entry together. It must NEVER change: a new GUID
; would make Windows see a second, separate program and leave the old one installed.
AppId={{2d78e401-2c1f-483f-9b67-51407892cac7}
AppName={#AppName}
AppVersion={#HubVersion}
AppPublisher=Lights Out (unofficial)
AppPublisherURL=https://lightsoutranked.com/
; There is no {userdownloads} constant (6.7.3 rejects it: "Unknown constant"), so the Downloads
; folder is resolved by GetDownloadsDir in [Code] below - it honours a relocated Downloads.
DefaultDirName={code:GetDownloadsDir}\LightsOut
PrivilegesRequired=lowest
; x64compatible (both directives) and MinVersion below all require Inno Setup 6.3+; build_hub.bat
; checks for it and the box has 6.7.3.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; MinVersion 6.3 = Windows 8.1. The bundled CPython 3.12 does not run on Windows 7; Inno 6 would
; otherwise allow Win7 SP1 and the hub would fail to launch there with a cryptic error.
MinVersion=6.3
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=LightsOut-Setup-{#HubVersion}
SetupIconFile=assets\hub.ico
UninstallDisplayIcon={app}\LightsOut.exe
UninstallDisplayName={#AppName}
; VersionInfoVersion must be numeric a.b.c.d, so it uses HubFileVersion (a pre-release HubVersion like
; "1.2.0-beta" would be rejected here). The display AppVersion / installer filename keep HubVersion.
VersionInfoVersion={#HubFileVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany=Lights Out (unofficial)
VersionInfoDescription=Lights Out installer (unofficial)
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[InstallDelete]
; Runs AFTER CloseApplications (so the old hub is not holding these) and BEFORE the [Files] copy.
; A PyInstaller onedir upgrade only overwrites files present in the NEW build; a .pyd, pak module or
; pythonXX.dll that the old build shipped and the new one dropped/renamed would otherwise linger in
; _internal\ forever and could be imported by mistake. Wiping _internal\ first gives the intended
; "replace the whole folder" semantics for this self-updating onedir app. {app}\LightsOut.exe and
; the shortcuts are left to the normal overwrite so a failed copy never leaves the app half-deleted.
Type: filesandordirs; Name: "{app}\_internal"
;
; The rename (Community Hub -> Lights Out) changed the exe name and the shortcut captions. Inno only
; overwrites files the NEW build actually ships, and it never removes a shortcut it did not just create,
; so without these three an upgraded install would keep a dead CommunityHub.exe in {app} and a SECOND
; "Community Hub" shortcut still pointing at it. AppId is unchanged, so this is the same program being
; upgraded in place - these lines are what make the rename look like a rename and not a second install.
; Safe to drop once no supported install predates the rename.
Type: files; Name: "{app}\CommunityHub.exe"
Type: files; Name: "{autoprograms}\Community Hub.lnk"
Type: files; Name: "{autodesktop}\Community Hub.lnk"

[Files]
; The whole PyInstaller one-folder output: LightsOut.exe plus _internal\ (python312.dll, the
; bundled tools\pak builder, icons). ignoreversion: force an overwrite instead of comparing file
; versions — most files (pak modules, icons) have no version, and even for LightsOut.exe we always
; want the build we just shipped, not "keep the higher version number".
Source: "..\dist\LightsOut\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\LightsOut.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\LightsOut.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Run]
; Interactive install: the usual "Start Lights Out" tick box on the last page.
Filename: "{app}\LightsOut.exe"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
; Silent update: only when the updater asked for it with /LAUNCHHUB=1.
Filename: "{app}\LightsOut.exe"; Flags: nowait; Check: LaunchHub

[Code]
{ ---------------------------------------------------------------- icon refresh

  The program folder path does not change between versions (and UsePreviousAppDir keeps an
  older install exactly where it was), so a shortcut created by an earlier build points at the
  same file it always did. When the artwork inside LightsOut.exe changes, Explorer keeps
  drawing the picture it already has for that path: after the 2.3.2 icon change a fresh
  install still showed the old mark on the desktop.

  Measured on the 2.3.2 install, to save the next person the same hunt:
    * the exe on disk had the new icon, in all seven sizes;
    * the shortcut asks for the target's own icon (IconLocation ",0"), not a copied one;
    * SHGetFileInfo on the .lnk ALREADY returned the new icon - the shell had it right;
    * `ie4uinit.exe -show` changed nothing.
  So nothing was stale except Explorer's on-screen view, and only a repaint was missing.
  Restarting Explorer fixed it; SHCNE_ASSOCCHANGED is the same repaint without the restart. }
const
  SHCNE_ASSOCCHANGED = $08000000;
  SHCNF_IDLIST       = $0000;

procedure SHChangeNotify(wEventId: Integer; uFlags: Cardinal; dwItem1, dwItem2: Cardinal);
  external 'SHChangeNotify@shell32.dll stdcall';

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { ssPostInstall runs after [Icons], so the shortcut exists by the time Explorer is told.
    Fires on the silent update path too, which is how most people get a new build. }
  if CurStep = ssPostInstall then
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, 0, 0);
end;

function GetDownloadsDir(Param: String): String;
var
  Path: String;
begin
  { The user's Downloads folder. Inno has no constant for it, so read the known folder's own
    registry entry - FOLDERID_Downloads, whose value name is that GUID - and fall back to the
    default location if the shell has not written it. "Shell Folders" is used rather than
    "User Shell Folders" because it always holds an already-expanded path. The braces here are a
    Pascal comment; the GUID below is inside a string literal, where braces are just characters. }
  if not RegQueryStringValue(HKEY_CURRENT_USER,
       'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders',
       '{374DE290-123F-4565-9164-39C4925E467B}', Path) or (Path = '') then
    Path := ExpandConstant('{userprofile}\Downloads');
  Result := Path;
end;

function LaunchHub: Boolean;
begin
  { Only launch from the silent-update path. WizardSilent guards against an interactive install that
    happened to be passed /LAUNCHHUB=1 also firing this entry on top of the postinstall tick box. }
  Result := WizardSilent and (ExpandConstant('{param:LAUNCHHUB|0}') = '1');
end;
