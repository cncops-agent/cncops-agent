Unicode true

; ============================================================================
;  cncops-demo NSIS 安装脚本
;  功能:把 PyInstaller 生成的 onedir 包安装到 Program Files,
;        创建「开始菜单 + 桌面」快捷方式,并带标准卸载程序.
;  编译:在有 NSIS 的机器上执行  build_installer.bat
;  产物:cncops-demo-setup.exe
;  说明:源目录默认 D:\Factory Project\cncops-agent\dist\cncops-demo
;        (可在编译时用 /DSRC="..." 覆盖,见 build_installer.bat)
; ============================================================================

!ifndef SRC
  !define SRC "D:\Factory Project\cncops-agent\dist\cncops-demo"
!endif

!define APPNAME   "cncops-demo"
!define APPFULL   "CNCOPS 车间设备智能诊断助手"
!define VERSION   "1.0.0"
!define PUBLISHER "cncops-agent 项目"
!define EXE       "cncops-demo.exe"

Name    "${APPFULL}"
OutFile "cncops-demo-setup.exe"
InstallDir "$PROGRAMFILES64\${APPNAME}"
InstallDirRegKey HKLM "Software\${APPNAME}" "InstallDir"
RequestExecutionLevel admin          ; 写 Program Files 需要管理员权限
SetCompressor /SOLID lzma            ; 最大压缩,减小安装包体积

; ---- 界面(现代 UI) ----
!include "MUI2.nsh"
!define MUI_ABORTWARNING
!define MUI_ICON   "${__FILEDIR__}\cncops-demo.ico"
!define MUI_UNICON "${__FILEDIR__}\cncops-demo.ico"

; 完成页:勾选「立即启动」+「查看使用说明」
!define MUI_FINISHPAGE_RUN            "$INSTDIR\${EXE}"
!define MUI_FINISHPAGE_RUN_TEXT      "立即启动 CNCOPS 演示"
!define MUI_FINISHPAGE_SHOWREADME    "$INSTDIR\使用说明.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "查看使用说明"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_LANGUAGE "SimpChinese"

; ============================================================================
;  安装段
; ============================================================================
Section "主程序" SEC_MAIN
  SetOutPath "$INSTDIR"
  ; 递归打包整个 onedir 目录(含 _internal 依赖,config,web,使用说明.txt)
  File /r "${SRC}\*.*"
  ; 应用图标(V2 自适应):随程序安装,供快捷方式显式引用
  File /oname=cncops-demo.ico "${__FILEDIR__}\cncops-demo.ico"

  ; ---- 卸载信息写入注册表(控制面板「卸载或更改程序」) ----
  WriteRegStr HKLM "Software\${APPNAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayName"     "${APPFULL}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "DisplayVersion"  "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "Publisher"       "${PUBLISHER}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}" "NoRepair" 1

  ; ---- 卸载程序本体 ----
  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; ---- 快捷方式(开始菜单 + 桌面);工作目录设为安装目录 ----
  CreateDirectory "$SMPROGRAMS\${APPFULL}"
  CreateShortCut "$SMPROGRAMS\${APPFULL}\${APPFULL}.lnk" "$INSTDIR\${EXE}" "" "$INSTDIR\cncops-demo.ico" 0 SW_SHOWNORMAL "" "$INSTDIR"
  CreateShortCut "$SMPROGRAMS\${APPFULL}\使用说明.lnk"     "$INSTDIR\使用说明.txt"
  CreateShortCut "$DESKTOP\${APPFULL}.lnk"                 "$INSTDIR\${EXE}" "" "$INSTDIR\cncops-demo.ico" 0 SW_SHOWNORMAL "" "$INSTDIR"
SectionEnd

; ============================================================================
;  卸载段
; ============================================================================
Section "Uninstall"
  ; 程序文件(含 _internal 依赖)
  RMDir /r "$INSTDIR"

  ; 快捷方式
  Delete "$DESKTOP\${APPFULL}.lnk"
  RMDir  /r "$SMPROGRAMS\${APPFULL}"

  ; 卸载注册表项
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"
  DeleteRegKey HKLM "Software\${APPNAME}"

  ; 注:用户数据(%LOCALAPPDATA%\cncops-demo 中的诊断记录/数据库)刻意保留,不删除,
  ;     避免误删用户现场产生的演示数据.如需彻底清理,手动删除该目录即可.
SectionEnd
