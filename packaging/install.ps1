# 找岗位助手 - Windows 安装助手（由 安装.cmd 启动）。
# 作用：1) 检测杀毒软件，告诉你被拦截时怎么放行  2) 去掉系统给下载文件打的"来自网络"标记  3) 打开程序
# 不会修改任何杀毒软件的设置，只检测和提示。
$dir = $PSScriptRoot
$exe = Join-Path $dir '找岗位助手.exe'
$Host.UI.RawUI.WindowTitle = '找岗位助手 安装助手'

Write-Host ''
Write-Host '===== 找岗位助手 安装助手 ====='
Write-Host ''
Write-Host '[1/3] 检测杀毒软件 ...'
$av = @()
try { $av = @((Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct).displayName | Sort-Object -Unique) } catch {}
if ($av.Count -gt 0) {
  foreach ($name in $av) { Write-Host "   已检测到：$name" }
  Write-Host ''
  Write-Host '   本程序没有商业代码签名，杀毒软件偶尔会误报，甚至直接隔离它。如果弹出拦截或风险提示：'
  Write-Host '     - 选「允许」「信任」或「加入白名单」，并把本文件夹加入信任区'
  Write-Host '     - Windows 自带的 Microsoft Defender：Windows 安全中心 → 病毒和威胁防护 → 保护历史记录，'
  Write-Host '       找到本程序点「允许」；或 管理设置 → 排除项 → 添加本文件夹'
} else {
  Write-Host '   没有检测到杀毒软件。'
}

Write-Host ''
Write-Host '[2/3] 解除系统隔离 ...'
if (-not (Test-Path -LiteralPath $exe)) {
  Write-Host ''
  Write-Host '   没找到 找岗位助手.exe。可能的原因：'
  Write-Host '     - 压缩包没有完整解压：请先右键「全部解压缩」，再运行本文件'
  Write-Host '     - 被杀毒软件当作风险文件隔离或删除了：请到杀毒软件的隔离区或保护历史记录里恢复它并选「信任」，再重新运行本文件'
  Read-Host '按回车键关闭窗口' | Out-Null
  exit 1
}
Get-ChildItem -LiteralPath $dir -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Write-Host '   已解除。'

Write-Host ''
Write-Host '[3/3] 完成。'
$go = Read-Host '现在打开找岗位助手？回车 = 打开，输入 n 再回车 = 不打开'
if ($go -ne 'n') { Start-Process -FilePath $exe }
Read-Host '按回车键关闭窗口' | Out-Null
