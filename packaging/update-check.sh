#!/usr/bin/env bash
# Run the app's update helper, the copy that ships in a build: packaging/update-check.sh <built-app-or-folder>
#   macOS:   <built-app-or-folder> is the bundle, dist/找岗位助手.app (helper: djs/apply.sh)
#   Windows: it is the folder, dist/dear-job-seeker (helper: djs/apply.ps1)
# The build is copied twice into a temp folder: as "installed" and as "staged" (the update, with a file added). The helper
# is run against a process that ends after about a second and the swap must have happened: the staged build in place, the
# old one in previous/, the result file saying ok, the app started again. No window opens: on macOS `open` is a shim that
# logs the call, on Windows the staged folder holds a launched.cmd that writes a marker. Windows also runs the case of a
# program of the installed folder that stays running: result "locked", that program named, the folder as it was.
set -euo pipefail
src="${1:?built app or folder}"
fail() { echo "update-check: $*" >&2; exit 1; }
[ -d "$src" ] || fail "$src is not a folder"
src=$(cd "$src" && pwd)
name=$(basename "$src")

mac=false
[ "$(uname -s)" = Darwin ] && mac=true
script=apply.ps1
$mac && script=apply.sh
helper=$(find "$src" -name "$script" -print -quit)
[ -n "$helper" ] || fail "$script is not in the build"

tmp=$(mktemp -d)
holder=""
cleanup() {
  [ -z "$holder" ] || { kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null; } || true
  rm -rf "$tmp" 2>/dev/null || true
}
trap cleanup EXIT
work="$tmp/更新 check"   # a space and Chinese in the path, as on a user's computer
installed="$work/installed/$name" staged="$work/staged/$name"
mkdir -p "$work/installed" "$work/staged"

result_is() {  # result_is <ok: true|false> <code>
  grep -Eq "\"ok\" *: *$1" "$work/last.json" && grep -Eq "\"code\" *: *\"$2\"" "$work/last.json" ||
    fail "the result file should say ok=$1 code=$2: $(cat "$work/last.json" 2>/dev/null || echo 'no result file')"
}
swapped() {  # the staged build is installed, the old one is in <previous>
  [ -f "$installed/update-check-new" ] && [ ! -e "$installed/update-check-old" ] || fail "the staged build is not in place"
  [ -f "$1/$name/update-check-old" ] && [ ! -e "$1/$name/update-check-new" ] || fail "the old build is not in $1"
}

if $mac; then
  ditto "$src" "$installed"
  ditto "$src" "$staged"
  echo old > "$installed/update-check-old"
  echo new > "$staged/update-check-new"
  xattr -w com.apple.quarantine "0083;00000000;Safari;" "$staged"   # what a download carries: the helper must lift it
  mkdir "$work/bin"
  printf '#!/bin/sh\necho "$*" >> "$OPEN_LOG"\n' > "$work/bin/open"
  chmod +x "$work/bin/open"
  export OPEN_LOG="$work/open.log"
  pid=$(sh -c 'sleep 1 >/dev/null 2>&1 & echo $!')   # not our child: a child that has ended would still answer kill -0
  out=$(PATH="$work/bin:$PATH" /bin/sh "$helper" "$pid" "$installed" "$staged" "$work/previous" "$work/last.json" 9.9.9 2>&1) ||
    fail "the helper failed: $out"
  swapped "$work/previous"
  result_is true ok
  [ "$(cat "$OPEN_LOG" 2>/dev/null)" = "$installed" ] || fail "the app was not started again: $(cat "$OPEN_LOG" 2>/dev/null)"
  [ -z "$(xattr -lr "$installed" | grep com.apple.quarantine || true)" ] || fail "the quarantine mark is still there"
else
  win() { cygpath -wl "$1"; }   # the long form: Get-Process reports paths that way, the runner's temp folder has a short one
  apply() {  # apply <pid> <folder for the previous build>
    APPLY_WAIT_SECONDS="${APPLY_WAIT_SECONDS:-60}" timeout 180 powershell.exe -NoProfile -ExecutionPolicy Bypass \
      -File "$(win "$helper")" -ProcessId "$1" -AppDir "$(win "$installed")" -NewDir "$(win "$staged")" \
      -PreviousDir "$(win "$2")" -Result "$(win "$work/last.json")" -Version 9.9.9 -Exe launched.cmd 2>&1
  }
  sleeper() {  # the Windows pid of a process of its own that ends after about a second
    powershell.exe -NoProfile -Command \
      "(Start-Process -PassThru -WindowStyle Hidden -FilePath ping -ArgumentList '-n','2','127.0.0.1').Id" | tr -d '\r'
  }
  launched() {  # wait for the marker the stub writes when the helper starts the app again
    for _ in $(seq 40); do [ -f "$installed/launched.marker" ] && return 0; sleep 0.5; done
    fail "the app was not started again ($installed/launched.marker)"
  }
  cp -R "$src" "$installed"
  cp -R "$src" "$staged"
  echo old > "$installed/update-check-old"
  echo new > "$staged/update-check-new"
  for dir in "$installed" "$staged"; do   # a stand-in for the window's program
    printf '@echo off\r\necho launched> "%%~dp0launched.marker"\r\n' > "$dir/launched.cmd"
  done

  out=$(apply "$(sleeper)" "$work/previous") || fail "the helper failed: $out"
  swapped "$work/previous"
  result_is true ok
  launched

  # An assistant's djs.exe keeps running from the folder: the swap waits (APPLY_WAIT_SECONDS), then names it and leaves the
  # folder as it was. The stand-in is a copy of `sleep` (with the runtime it needs) that runs from inside the folder.
  rm -f "$work/last.json" "$installed/launched.marker"
  echo mine > "$installed/update-check-locked"
  bin=$(dirname "$(command -v sleep)")
  cp "$bin/sleep.exe" "$installed/holder.exe"
  cp "$bin/msys-2.0.dll" "$installed/"
  (cd "$installed" && exec ./holder.exe 300) &
  holder=$!
  sleep 1
  kill -0 "$holder" 2>/dev/null || fail "the stand-in for a running program did not start"
  out=$(APPLY_WAIT_SECONDS=5 apply "$(sleeper)" "$work/previous-locked") || true
  result_is false locked
  grep -q holder "$work/last.json" || fail "the running program is not named: $(cat "$work/last.json")"
  [ -f "$installed/update-check-locked" ] && [ ! -e "$work/previous-locked/$name" ] ||
    fail "the installed folder was touched although a program of it runs: $out"
  launched
fi
echo "update-check: ok"
