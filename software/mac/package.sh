#!/bin/zsh
# Builds a self-contained "PC Stats Panel.app" and a .dmg for people who do not want a Terminal.
#
#   zsh mac/package.sh                                   ad-hoc signed (only runs on your own Mac)
#   zsh mac/package.sh --identity "Developer ID Application: Your Name (TEAMID)"
#   zsh mac/package.sh --identity "..." --notarize pcstats      also notarize + staple the app and the .dmg
#   zsh mac/package.sh --version 2.5
#   zsh mac/package.sh --app "build/export/PC Stats Panel.app"   wrap an app built and signed elsewhere (mac/release.sh)
#
# --notarize takes a notarytool keychain profile, created once with
#   xcrun notarytool store-credentials pcstats --apple-id you@example.com --team-id TEAMID --password <app-specific password>
# Output: dist/PC-Stats-Panel-<version>.dmg (and build/PC Stats Panel.app).
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$SRC/build"
DIST="$SRC/dist"
IDENTITY="-"
PROFILE=""
GIVEN_APP=""
VERSION="$(grep -o 'CFBundleVersion</key><string>[^<]*' "$SRC/mac/install.sh" | head -1 | sed 's/.*<string>//')"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --identity) IDENTITY="$2"; shift 2 ;;
    --notarize) PROFILE="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --app) GIVEN_APP="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[[ -n "$VERSION" ]] || VERSION="1.0"
APP="$BUILD/PC Stats Panel.app"
DMG="$DIST/PC-Stats-Panel-$VERSION.dmg"

if [[ -n "$GIVEN_APP" ]]; then
  # An app that is already built and signed (mac/release.sh): only the disk image, signed like the app.
  APP="$GIVEN_APP"
  IDENTITY="$(codesign -dv --verbose=2 "$APP" 2>&1 | sed -n 's/^Authority=\(Developer ID Application: .*\)$/\1/p' | head -1)"
  [[ -n "$IDENTITY" ]] || IDENTITY="-"
  mkdir -p "$BUILD" "$DIST"
  echo "== 1-4/5 using $APP  (signed by: $IDENTITY)"
else
echo "== 1/5 bundle  (version $VERSION)"
rm -rf "$APP" "$BUILD/stage"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app" "$APP/Contents/Resources/mac" "$DIST"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PC Stats Panel</string>
  <key>CFBundleDisplayName</key><string>PC Stats Panel</string>
  <key>CFBundleIdentifier</key><string>com.pcstatsdock.agent</string>
  <key>CFBundleExecutable</key><string>PCStatsPanel</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHumanReadableCopyright</key><string>PC Stats Dock</string>
</dict></plist>
EOF
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$SRC/agent/" "$APP/Contents/Resources/app/agent/"
rsync -a "$SRC/dashboard/" "$APP/Contents/Resources/app/dashboard/"
cp "$SRC/mac/dock-admin.sh" "$APP/Contents/Resources/mac/"
[[ -f "$SRC/mac/AdminIcon.png" ]] && cp "$SRC/mac/AdminIcon.png" "$APP/Contents/Resources/mac/"
ICONSET="$BUILD/AppIcon.iconset"; rm -rf "$ICONSET"; mkdir -p "$ICONSET"
if [[ -f "$SRC/mac/AppIcon.png" ]]; then          # your own artwork: a 1024x1024 PNG with a transparent background
  sips -z 512 512 "$SRC/mac/AppIcon.png" --out "$ICONSET/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$SRC/mac/AppIcon.png" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
else
  python3 "$SRC/mac/make-icon.py" "$ICONSET/icon_512x512.png"
fi
for s in 16 32 128 256; do sips -z $s $s "$ICONSET/icon_512x512.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null; done
cp "$ICONSET/icon_32x32.png" "$ICONSET/icon_16x16@2x.png"; cp "$ICONSET/icon_256x256.png" "$ICONSET/icon_128x128@2x.png"; cp "$ICONSET/icon_512x512.png" "$ICONSET/icon_256x256@2x.png"
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

echo "== 2/5 launcher (universal, bundle mode)"
clang -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=13.0 -DBUNDLED \
  -framework ApplicationServices -framework CoreFoundation -framework IOKit \
  -o "$APP/Contents/MacOS/PCStatsPanel" "$SRC/mac/launcher.c"

echo "== 3/5 sign  ($IDENTITY)"
if [[ "$IDENTITY" == "-" ]]; then
  codesign --force --sign - --identifier com.pcstatsdock.agent --options runtime "$APP"
else
  codesign --force --sign "$IDENTITY" --identifier com.pcstatsdock.agent --options runtime --timestamp "$APP"
fi
codesign --verify --strict --deep "$APP" && echo "   signature ok"

if [[ -n "$PROFILE" ]]; then
  echo "== 4/5 notarize the app (profile $PROFILE)"
  ZIP="$BUILD/PC-Stats-Panel-$VERSION.zip"
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
  xcrun stapler staple "$APP"
else
  echo "== 4/5 notarize: skipped (no --notarize PROFILE)"
fi
fi   # end of the build-it-here path

echo "== 5/5 disk image"
STAGE="$BUILD/stage"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/Read me first.txt" <<'EOF'
PC Stats Panel

1. Drag "PC Stats Panel" to Applications, then open it once. It installs itself as a login item
   and quits; nothing stays in your Dock.
2. Plug in the touch panel: the dashboard opens on it.
3. System Settings > Privacy & Security > Accessibility: switch on "PC Stats Panel"
   (key buttons and touch need it).
4. Temperatures need Homebrew's macmon (brew install macmon). Optional.

The "Stats Dock Admin" icon in your Dock opens the button editor (http://localhost:4400/admin).
To remove: open Terminal and run
  "/Applications/PC Stats Panel.app/Contents/MacOS/PCStatsPanel" --uninstall
then drag the app to the Trash.
EOF
rm -f "$DMG"
hdiutil create -volname "PC Stats Panel" -srcfolder "$STAGE" -ov -format UDZO -quiet "$DMG"
if [[ "$IDENTITY" != "-" ]]; then
  codesign --force --sign "$IDENTITY" --timestamp "$DMG" 2>/dev/null || echo "   (dmg left unsigned: that identity is not in the local keychain; the app inside is what Gatekeeper checks)"
  if [[ -n "$PROFILE" ]]; then
    xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait
    xcrun stapler staple "$DMG"
  fi
fi
echo
echo "   app: $APP"
echo "   dmg: $DMG  ($(du -h "$DMG" | cut -f1))"
[[ "$IDENTITY" == "-" ]] && echo "   (ad-hoc signed: fine for this Mac; other Macs need --identity and --notarize to open it without warnings)"
exit 0
