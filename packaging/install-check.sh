#!/usr/bin/env bash
# Check the installer helper in a staged download: packaging/install-check.sh <staged-dir>
#   macOS:   <staged-dir> holds 找岗位助手.app and 安装.command
#   Windows: <staged-dir> holds 找岗位助手.exe and 安装.cmd
# Marks the app as downloaded (quarantine attribute / Zone.Identifier stream), runs the helper with "no" typed at
# its questions, and requires the mark to be gone.
set -euo pipefail
dir="${1:?staged dir}"
fail() { echo "install-check: $*" >&2; exit 1; }

if [ "$(uname -s)" = Darwin ]; then
  app="$dir/找岗位助手.app"
  xattr -w com.apple.quarantine "0083;00000000;Safari;" "$app"
  xattr -w com.apple.quarantine "0083;00000000;Safari;" "$app/Contents/Info.plist"
  out=$(printf 'n\n' | bash "$dir/安装.command" 2>&1) || fail "helper failed: $out"
  echo "$out"
  [ -z "$(xattr -lr "$app" | grep com.apple.quarantine || true)" ] || fail "quarantine mark is still there"
else
  exe="$dir/找岗位助手.exe"
  win() { cygpath -w "$1"; }
  F=$(win "$exe") powershell -NoProfile -Command \
    'Set-Content -LiteralPath $env:F -Stream Zone.Identifier -Value "[ZoneTransfer]","ZoneId=3"'
  # The runner has no Security Center, so the "antivirus found" branch runs once against a made-up product
  sed "s#(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct).displayName#'Test AV','火绒安全软件'#" \
    "$dir/安装.cmd" > "$dir/安装-test.cmd"
  out=$(echo n | cmd.exe //c "$(win "$dir/安装-test.cmd")" 2>&1) || fail "helper failed: $out"
  rm "$dir/安装-test.cmd"
  case "$out" in *"已检测到：Test AV"*"已检测到：火绒安全软件"*"Microsoft Defender"*) ;; *) fail "antivirus branch: $out" ;; esac
  out=$(echo n | cmd.exe //c "$(win "$dir/安装.cmd")" 2>&1) || fail "helper failed: $out"
  echo "$out"
  case "$out" in *"[1/3]"*"[3/3]"*) ;; *) fail "helper did not run to its end" ;; esac
  F=$(win "$exe") powershell -NoProfile -Command \
    'if (Get-Content -LiteralPath $env:F -Stream Zone.Identifier -ErrorAction SilentlyContinue) { exit 1 }' ||
    fail "Zone.Identifier is still on the exe"
fi
echo "install-check: ok"
