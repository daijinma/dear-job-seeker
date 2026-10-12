#!/usr/bin/env bash
# Rebuild icon.icns and icon.ico from icon.svg (macOS: Chrome renders the SVG, sips scales it, iconutil packs it).
set -euo pipefail
cd "$(dirname "$0")"
chrome="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

"$chrome" --headless=new --disable-gpu --hide-scrollbars --default-background-color=00000000 \
  --window-size=1024,1024 --screenshot="$tmp/1024.png" "file://$PWD/icon.svg" >/dev/null 2>&1

mkdir "$tmp/icon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$tmp/1024.png" --out "$tmp/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$tmp/1024.png" --out "$tmp/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$tmp/icon.iconset" -o icon.icns

# Windows: PNGs of 16-256 px in one .ico (PNG entries, stdlib only)
for s in 16 24 32 48 64 128 256; do sips -z $s $s "$tmp/1024.png" --out "$tmp/$s.png" >/dev/null; done
python3 - "$tmp" <<'EOF'
import struct, sys
sizes = (16, 24, 32, 48, 64, 128, 256)
pngs = [open(f"{sys.argv[1]}/{s}.png", "rb").read() for s in sizes]
out = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
for s, data in zip(sizes, pngs):
    out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
open("icon.ico", "wb").write(out + b"".join(pngs))
EOF
echo "icon.icns and icon.ico rebuilt"
