#!/bin/zsh
# "Stats Dock Admin" in the macOS Dock: a tiny app that opens http://localhost:4400/admin in your browser.
# Idempotent. `dock-admin.sh --remove` takes it out again.
set -e
APP="$HOME/Applications/Stats Dock Admin.app"
URL="http://localhost:4400/admin"

dock_has() { defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Stats Dock Admin.app"; }

if [[ "$1" == "--remove" ]]; then
  rm -rf "$APP"
  if dock_has; then
    /usr/bin/python3 - <<'PY'
import plistlib, subprocess
raw = subprocess.run(["defaults", "export", "com.apple.dock", "-"], capture_output=True).stdout
d = plistlib.loads(raw)
d["persistent-apps"] = [a for a in d.get("persistent-apps", []) if "Stats Dock Admin.app" not in str(a.get("tile-data", {}).get("file-data", {}).get("_CFURLString", ""))]
subprocess.run(["defaults", "import", "com.apple.dock", "-"], input=plistlib.dumps(d), check=True)
PY
    killall Dock 2>/dev/null || true
  fi
  echo "   removed Stats Dock Admin from the Dock"
  exit 0
fi

mkdir -p "$HOME/Applications"
if [[ ! -d "$APP" ]]; then
  osacompile -o "$APP" -e "open location \"$URL\""
  /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.pcstatsdock.admin" "$APP/Contents/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.pcstatsdock.admin" "$APP/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleName 'Stats Dock Admin'" "$APP/Contents/Info.plist" 2>/dev/null || true
  # icon: dark tile with an orange gauge ring (drawn here so nothing else is needed)
  ICONSET="$(mktemp -d)/admin.iconset"; mkdir -p "$ICONSET"
  if [[ -f "$(dirname "$0")/AdminIcon.png" ]]; then        # your own artwork (1024x1024 PNG, transparent background)
    sips -z 512 512 "$(dirname "$0")/AdminIcon.png" --out "$ICONSET/icon_512x512.png" >/dev/null
  else
  /usr/bin/python3 - "$ICONSET/icon_512x512.png" <<'PY'
import math, struct, sys, zlib
N = 512
def px(x, y):
    # rounded square
    r = 96; cx, cy = N / 2, N / 2
    dx, dy = max(abs(x - cx) - (N / 2 - r), 0), max(abs(y - cy) - (N / 2 - r), 0)
    if math.hypot(dx, dy) > r: return (0, 0, 0, 0)
    col = (18, 25, 23)
    d = math.hypot(x - cx, y - cy + 12); ang = math.degrees(math.atan2(y - cy + 12, x - cx))
    # gauge ring: from 135° to 405° (through the top), 34 px thick, radius 150
    a = (ang + 360) % 360
    on_arc = 143 <= d <= 177 and (a >= 135 or a <= 45)
    lit = on_arc and (a >= 135 or a <= 10)           # filled part
    if on_arc: col = (224, 140, 76) if lit else (51, 65, 61)
    # needle towards 10°
    nx, ny = math.cos(math.radians(10)), math.sin(math.radians(10))
    t = (x - cx) * nx + (y - cy + 12) * ny; perp = abs(-(x - cx) * ny + (y - cy + 12) * nx)
    if 0 <= t <= 120 and perp <= 9 - 6 * t / 120: col = (230, 235, 232)
    if d <= 20: col = (230, 235, 232)
    if d <= 11: col = (18, 25, 23)
    return (*col, 255)
rows = []
for y in range(N):
    row = bytearray([0])
    for x in range(N):
        row += bytes(px(x + .5, y + .5))
    rows.append(bytes(row))
def chunk(t, b): return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
PY
  fi
  for s in 16 32 128 256; do sips -z $s $s "$ICONSET/icon_512x512.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null; done
  cp "$ICONSET/icon_32x32.png" "$ICONSET/icon_16x16@2x.png"; cp "$ICONSET/icon_256x256.png" "$ICONSET/icon_128x128@2x.png"; cp "$ICONSET/icon_512x512.png" "$ICONSET/icon_256x256@2x.png"
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns" 2>/dev/null || true
  touch "$APP"
  codesign --force --sign - "$APP" 2>/dev/null || true
  echo "   made $APP"
fi
if ! dock_has; then
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$APP</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
  killall Dock 2>/dev/null || true
  echo "   added Stats Dock Admin to the Dock"
else
  echo "   Stats Dock Admin already in the Dock"
fi
