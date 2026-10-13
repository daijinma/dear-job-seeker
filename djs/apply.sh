#!/bin/sh
# Swaps the app bundle for the staged one after the app has quit, then starts it.
# Arguments: window pid, app bundle, staged bundle, folder for the previous build, result file, new version.
# ASCII only: the result is a code the app turns into words at its next start.
pid=$1 app=$2 new=$3 prev=$4 out=$5 version=$6

done_with() {  # ok(true|false) code
    printf '{"ok":%s,"code":"%s","version":"%s","who":[]}\n' "$1" "$2" "$version" > "$out"
}

n=0
while kill -0 "$pid" 2>/dev/null; do  # up to 60 s (120 half-seconds; APPLY_TICKS shortens it for tests) for the app to quit
    n=$((n + 1))
    if [ "$n" -gt "${APPLY_TICKS:-120}" ]; then done_with false still_running; exit 1; fi
    sleep 0.5
done

name=$(basename "$app")
rm -rf "$prev"
mkdir -p "$prev" || { done_with false swap_failed; open "$app"; exit 1; }
if ! mv "$app" "$prev/$name"; then done_with false swap_failed; open "$app"; exit 1; fi
if ! ditto "$new" "$app"; then
    rm -rf "$app"
    mv "$prev/$name" "$app"
    done_with false swap_failed
    open "$app"
    exit 1
fi
xattr -dr com.apple.quarantine "$app" 2>/dev/null  # our own download has none; harmless when it has
done_with true ok
open "$app"
