#!/bin/bash
# 找岗位助手 - macOS 安装助手：解压后在文件夹里双击本文件。
# 作用：去掉系统给下载文件打的"来自网络"隔离标记（xattr），App 才不会被"无法验证开发者"拦住；
#       可选拷到「应用程序」；然后打开。
# 第一次双击本文件时若被系统拦下：右键 → 打开 → 打开，
# 或「系统设置 → 隐私与安全性」点「仍要打开」。只这一次。

cd "$(dirname "$0")" || exit 1
APP="$(ls -d *.app 2>/dev/null | head -1)"

# 没有终端（脚本被自动化测试调用）时不停下来等按键，也不打开 App
bye() { [ -t 0 ] && { echo; read -n 1 -s -r -p "按任意键关闭窗口…"; echo; }; exit "${1:-0}"; }

echo "===== 找岗位助手 安装助手 ====="
if [ -z "$APP" ]; then
  echo "没找到「找岗位助手.app」：请让本文件和它放在同一个文件夹里（先把压缩包完整解压）。"
  bye 1
fi

xattr -dr com.apple.quarantine "$APP" && echo "已解除系统隔离：$APP"

read -r -p "拷贝到「应用程序」文件夹？(y/N) " ans
if [[ "$ans" =~ ^[yY] ]]; then
  if rm -rf "/Applications/$APP" && cp -R "$APP" /Applications/; then
    APP="/Applications/$APP"
    xattr -dr com.apple.quarantine "$APP"
    echo "已安装到「应用程序」。"
  else
    echo "拷贝失败（可能没有权限）：可手动把 $APP 拖进「应用程序」，或直接双击它运行。"
  fi
fi

echo "完成。"
[ -t 0 ] && open "$APP"
bye
