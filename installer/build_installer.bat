@echo off
chcp 65001 >nul
REM ===========================================================================
REM  编译 NSIS 安装包：生成 cncops-demo-setup.exe
REM  前置条件：本机已安装 NSIS（https://nsis.sourceforge.io/Download）
REM  用法：双击本文件，或在命令行执行
REM ===========================================================================

SETLOCAL

REM 固定工作目录到 installer\，确保 nsi 里的相对图标路径可被 makensis 解析
cd /d "%~dp0"

SET "EXE=cncops-demo.exe"

REM 自动定位 makensis.exe（常见安装路径）
SET "NSIS="
IF EXIST "C:\Program Files (x86)\NSIS\makensis.exe" SET "NSIS=C:\Program Files (x86)\NSIS\makensis.exe"
IF EXIST "C:\Program Files\NSIS\makensis.exe"        SET "NSIS=C:\Program Files\NSIS\makensis.exe"
where makensis >nul 2>&1 && SET "NSIS=makensis.exe"

IF NOT DEFINED NSIS (
  echo [错误] 未找到 makensis.exe，请先安装 NSIS：
  echo         https://nsis.sourceforge.io/Download
  echo         或修改本脚本中 NSIS 路径。
  pause
  EXIT /B 1
)

REM 源目录：定位到 installer\ 上一级的 dist\cncops-demo
SET "SRC=%~dp0..\dist\cncops-demo"
IF NOT EXIST "%SRC%\%EXE%" (
  echo [错误] 未找到打包源目录：%SRC%
  echo         请先运行 PyInstaller 构建（见 README / 项目总结文档），
  echo         或手动修改本脚本 SRC 指向正确的 onedir 目录。
  pause
  EXIT /B 1
)

echo [信息] 源目录：%SRC%
echo [信息] 正在编译安装包...
"%NSIS%" /DSRC="%SRC%" "%~dp0cncops-demo.nsi"

IF EXIST "%~dp0cncops-demo-setup.exe" (
  echo [完成] 已生成：%~dp0cncops-demo-setup.exe
) ELSE (
  echo [失败] 编译未产出 setup.exe，请检查上方报错。
)
pause
ENDLOCAL
