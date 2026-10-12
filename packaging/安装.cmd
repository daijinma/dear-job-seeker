@echo off
rem 找岗位助手 - Windows 安装助手：解压后在文件夹里双击本文件。
rem 作用：1) 检测杀毒软件，告诉你被拦截时怎么放行  2) 去掉系统给下载文件打的"来自网络"标记  3) 打开程序
rem 本文件以 UTF-8 保存（发布时转成 CRLF 换行），下面的 chcp 让窗口按 UTF-8 显示中文。
rem 不会修改任何杀毒软件的设置，只检测和提示。
chcp 65001 >nul
setlocal
set "DJS_DIR=%~dp0"
cd /d "%DJS_DIR%"
title 找岗位助手 安装助手

echo.
echo ===== 找岗位助手 安装助手 =====
echo.
echo [1/3] 检测杀毒软件 ...
set "AV="
for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "try { (Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct).displayName | Sort-Object -Unique } catch {}"`) do (
  set "AV=1"
  echo    已检测到：%%a
)
if defined AV (
  echo.
  echo    本程序没有商业代码签名，杀毒软件偶尔会误报，甚至直接隔离它。如果弹出拦截或风险提示：
  echo      - 选「允许」「信任」或「加入白名单」，并把本文件夹加入信任区
  echo      - Windows 自带的 Microsoft Defender：Windows 安全中心 → 病毒和威胁防护 → 保护历史记录，
  echo        找到本程序点「允许」；或 管理设置 → 排除项 → 添加本文件夹
) else (
  echo    没有检测到杀毒软件。
)

echo.
echo [2/3] 解除系统隔离 ...
if not exist "找岗位助手.exe" (
  echo.
  echo    没找到 找岗位助手.exe。可能的原因：
  echo      - 压缩包没有完整解压：请先右键「全部解压缩」，再运行本文件
  echo      - 被杀毒软件当作风险文件隔离或删除了：请到杀毒软件的隔离区或保护历史记录里恢复它并选「信任」，再重新运行本文件
  goto :end
)
powershell -NoProfile -Command "Get-ChildItem -LiteralPath $env:DJS_DIR -Recurse -File | Unblock-File -ErrorAction SilentlyContinue"
echo    已解除。

echo.
echo [3/3] 完成。
set "GO="
set /p "GO=现在打开找岗位助手？回车 = 打开，输入 n 再回车 = 不打开："
if /i "%GO%"=="n" goto :end
start "" "%DJS_DIR%找岗位助手.exe"

:end
echo.
pause
