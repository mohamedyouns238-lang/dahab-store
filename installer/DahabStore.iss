; Inno Setup — يبني DahabStore-Setup.exe من مجلد dist\DahabStore
; تثبيت لمستخدم ويندوز الحالي (لا يحتاج صلاحيات مدير). بيانات المحل في %APPDATA%\DahabStore
; ولا يلمسها المثبّت ولا الإزالة ولا التحديث أبدًا.
#define AppName "Dahab Store"
#define AppVer "1.0.0"
#define AppExe "DahabStore.exe"

[Setup]
AppId={{6B1E5F52-9C0A-4D55-B3D7-DA4AB5700001}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=Dahab Store
DefaultDirName={autopf}\Dahab Store
DefaultGroupName=Dahab Store
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\output
OutputBaseFilename=DahabStore-Setup
SetupIconFile=..\assets\dahab.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "..\dist\DahabStore\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Dahab Store"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\Dahab Store"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch Dahab Store"; Flags: nowait postinstall skipifsilent

; لا يوجد قسم [UninstallDelete] لمجلد البيانات عمدًا: الإزالة لا تحذف بيانات المحل.
