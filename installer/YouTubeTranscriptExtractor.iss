; Inno Setup script — wraps the PyInstaller onedir bundle into a single setup.exe.
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\YouTubeTranscriptExtractor.iss
; Version can be overridden:  ISCC ... /DMyAppVersion=1.2.3
; The exe is built first by `pyinstaller YouTubeTranscriptExtractor.spec` into
; dist\YouTubeTranscriptExtractor\ (onedir). The ~5.3GB AI model is NOT bundled —
; the app downloads it to %LOCALAPPDATA% on first use, so the install stays ~340MB.

#ifndef MyAppVersion
#define MyAppVersion "1.0.1"
#endif
#define MyAppName "YouTube 자막 추출기"
#define MyAppPublisher "MazeLine"
#define MyAppURL "https://mazeline.tech"
#define MyAppExeName "YouTubeTranscriptExtractor.exe"
#define MyBundleDir "..\dist\YouTubeTranscriptExtractor"

[Setup]
AppId={{A7E5C2D9-4F31-4B8A-9E62-1C3D7F0A5B84}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\YouTubeTranscriptExtractor
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\dist\installer
OutputBaseFilename=YouTubeTranscriptExtractor-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyBundleDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
