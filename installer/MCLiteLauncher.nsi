; ============================================================
;  MCLite Launcher - NSIS Installer Script
;  Installer Windows untuk MCLite Launcher (Minecraft Bedrock GDK)
;  Bisa pilih drive/disk tujuan instalasi
; ============================================================

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

; ── Metadata ───────────────────────────────────────────────
!define APP_NAME     "MCLite Launcher"
!define APP_VERSION  "1.0.0"
!define APP_EXE      "MCLiteLauncher.exe"
!define APP_REGKEY   "Software\MCLiteLauncher"
!define APP_UNINST   "Software\Microsoft\Windows\CurrentVersion\Uninstall\MCLiteLauncher"
!define PUBLISHER    "MCLite Project"
!define APP_URL      "https://github.com/YOUR_USERNAME/MCLiteLauncher"

; ── General ────────────────────────────────────────────────
Name                "${APP_NAME} ${APP_VERSION}"
OutFile             "MCLiteLauncher-Setup-v${APP_VERSION}.exe"
InstallDir          "$PROGRAMFILES64\MCLiteLauncher"
InstallDirRegKey    HKCU "${APP_REGKEY}" "InstallDir"
RequestExecutionLevel admin
BrandingText        "${APP_NAME} v${APP_VERSION}"
SetCompressor       /SOLID lzma
SetCompressorDictSize 64

; ── MUI Settings ───────────────────────────────────────────
!define MUI_ABORTWARNING
!define MUI_ICON                    "..\assets\icon.ico"
!define MUI_UNICON                  "..\assets\icon.ico"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP      "..\assets\header.bmp"
!define MUI_WELCOMEFINISHPAGE_BITMAP "..\assets\wizard.bmp"
!define MUI_WELCOMEPAGE_TITLE       "Selamat Datang di ${APP_NAME} Setup"
!define MUI_WELCOMEPAGE_TEXT        "Installer ini akan memasang MCLite Launcher di komputer Anda.$\r$\n$\r$\nMCLite Launcher adalah launcher ringan untuk Minecraft Bedrock Edition (GDK) dengan fitur:$\r$\n  • Memory Leak Fix otomatis (GDK issue sejak v1.21.120+)$\r$\n  • Performance Boost (CPU priority, timer resolution)$\r$\n  • FPS Counter Overlay di dalam game$\r$\n  • Fitur pengurangan lag$\r$\n$\r$\nKlik Berikutnya untuk melanjutkan."
!define MUI_FINISHPAGE_RUN          "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT     "Jalankan MCLite Launcher sekarang"
!define MUI_FINISHPAGE_LINK        "GitHub Repository"
!define MUI_FINISHPAGE_LINK_LOCATION "${APP_URL}"

; ── Pages ──────────────────────────────────────────────────
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE         "..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

; ── Language ───────────────────────────────────────────────
!insertmacro MUI_LANGUAGE "Indonesian"
!insertmacro MUI_LANGUAGE "English"

; ── Installer Sections ─────────────────────────────────────
Section "MCLite Launcher (Wajib)" SecMain
    SectionIn RO

    ; Cek Windows 10 64-bit minimal
    ${IfNot} ${AtLeastWin10}
        MessageBox MB_ICONSTOP "MCLite Launcher membutuhkan Windows 10 atau lebih baru."
        Abort
    ${EndIf}

    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP "MCLite Launcher hanya mendukung sistem 64-bit."
        Abort
    ${EndIf}

    SetOutPath "$INSTDIR"
    SetOverwrite on

    ; Copy semua file dari dist/
    File /r "..\dist\MCLiteLauncher\*.*"

    ; Simpan info instalasi
    WriteRegStr HKCU "${APP_REGKEY}" "InstallDir" "$INSTDIR"
    WriteRegStr HKCU "${APP_REGKEY}" "Version"    "${APP_VERSION}"

    ; Uninstall entry di Control Panel
    WriteRegStr   HKLM "${APP_UNINST}" "DisplayName"      "${APP_NAME}"
    WriteRegStr   HKLM "${APP_UNINST}" "DisplayVersion"   "${APP_VERSION}"
    WriteRegStr   HKLM "${APP_UNINST}" "Publisher"        "${PUBLISHER}"
    WriteRegStr   HKLM "${APP_UNINST}" "URLInfoAbout"     "${APP_URL}"
    WriteRegStr   HKLM "${APP_UNINST}" "InstallLocation"  "$INSTDIR"
    WriteRegStr   HKLM "${APP_UNINST}" "UninstallString"  "$INSTDIR\Uninstall.exe"
    WriteRegStr   HKLM "${APP_UNINST}" "DisplayIcon"      "$INSTDIR\${APP_EXE}"
    WriteRegDWORD HKLM "${APP_UNINST}" "NoModify"         1
    WriteRegDWORD HKLM "${APP_UNINST}" "NoRepair"         1
    WriteRegStr   HKLM "${APP_UNINST}" "EstimatedSize"    "45000"

    ; Tulis Uninstaller
    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Shortcut Desktop" SecDesktop
    CreateShortcut "$DESKTOP\MCLite Launcher.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "Shortcut Start Menu" SecStartMenu
    CreateDirectory "$SMPROGRAMS\MCLite Launcher"
    CreateShortcut "$SMPROGRAMS\MCLite Launcher\MCLite Launcher.lnk" \
        "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
    CreateShortcut "$SMPROGRAMS\MCLite Launcher\Uninstall.lnk" \
        "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Autostart saat Windows" SecAutostart
    WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Run" \
        "MCLiteLauncher" '"$INSTDIR\${APP_EXE}" --minimized'
SectionEnd

; ── Deskripsi Sections ─────────────────────────────────────
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}      "File utama MCLite Launcher (wajib)."
    !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop}   "Buat shortcut di Desktop."
    !insertmacro MUI_DESCRIPTION_TEXT ${SecStartMenu} "Buat folder di Start Menu."
    !insertmacro MUI_DESCRIPTION_TEXT ${SecAutostart} "Jalankan MCLite otomatis saat Windows start (minimize ke tray)."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ── Uninstaller ────────────────────────────────────────────
Section "Uninstall"
    ; Hapus file
    RMDir /r "$INSTDIR"

    ; Hapus shortcut
    Delete "$DESKTOP\MCLite Launcher.lnk"
    RMDir /r "$SMPROGRAMS\MCLite Launcher"

    ; Hapus registry
    DeleteRegKey HKCU  "${APP_REGKEY}"
    DeleteRegKey HKLM  "${APP_UNINST}"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "MCLiteLauncher"

    MessageBox MB_ICONINFORMATION "MCLite Launcher telah dihapus dari komputer Anda."
SectionEnd

; ── Functions ──────────────────────────────────────────────
Function .onInit
    ; Cek apakah sudah terinstall
    ReadRegStr $R0 HKCU "${APP_REGKEY}" "InstallDir"
    ${If} $R0 != ""
        MessageBox MB_YESNO|MB_ICONQUESTION \
            "MCLite Launcher sudah terinstall di $R0.$\r$\nApakah Anda ingin menginstall ulang / update?" \
            IDYES +2
        Abort
    ${EndIf}
FunctionEnd
