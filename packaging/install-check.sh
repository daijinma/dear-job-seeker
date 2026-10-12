#!/usr/bin/env bash
# Check the installer helper in a staged download: packaging/install-check.sh <staged-dir>
#   macOS:   <staged-dir> holds 找岗位助手.app and 安装.command
#   Windows: <staged-dir> holds 找岗位助手.exe, 安装.cmd and install.ps1
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
  # The runner has no Security Center, so the "antivirus found" branch runs once against two made-up products.
  # (The text is Chinese and goes to the console as Unicode; piped into this script it is not readable, so the
  # checks below match the ASCII parts only.)
  run() { echo n | timeout 120 cmd.exe //c "$(win "$dir/安装.cmd")" 2>&1; }
  cp "$dir/install.ps1" "$dir/install.ps1.orig"
  sed "s#(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct).displayName#'Test AV','Second AV'#" \
    "$dir/install.ps1.orig" > "$dir/install.ps1"
  out=$(run) || { mv "$dir/install.ps1.orig" "$dir/install.ps1"; fail "helper failed: $out"; }
  mv "$dir/install.ps1.orig" "$dir/install.ps1"
  case "$out" in *"Test AV"*"Second AV"*"Microsoft Defender"*"[3/3]"*) ;; *) fail "antivirus branch: $out" ;; esac
  out=$(run) || fail "helper failed: $out"
  echo "$out"
  case "$out" in *"[1/3]"*"[3/3]"*) ;; *) fail "helper did not run to its end" ;; esac
  F=$(win "$exe") powershell -NoProfile -Command \
    'if (Get-Content -LiteralPath $env:F -Stream Zone.Identifier -ErrorAction SilentlyContinue) { exit 1 }' ||
    fail "Zone.Identifier is still on the exe"
fi
echo "install-check: ok"
