#!/bin/zsh
# Xcode build phase: puts the agent, the dashboard, the Dock-shortcut builder and the icon into the app bundle
# (Contents/Resources/app, Contents/Resources/mac, AppIcon.icns). Runs before code signing, so it all gets sealed.
set -e
SOFTWARE="$SRCROOT/../.."
RES="$TARGET_BUILD_DIR/$UNLOCALIZED_RESOURCES_FOLDER_PATH"
mkdir -p "$RES/app" "$RES/mac"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$SOFTWARE/agent/" "$RES/app/agent/"
rsync -a --delete "$SOFTWARE/dashboard/" "$RES/app/dashboard/"
cp "$SOFTWARE/mac/dock-admin.sh" "$RES/mac/"
[[ -f "$SOFTWARE/mac/AdminIcon.png" ]] && cp "$SOFTWARE/mac/AdminIcon.png" "$RES/mac/"
ICONSET="$DERIVED_FILE_DIR/AppIcon.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
if [[ -f "$SOFTWARE/mac/AppIcon.png" ]]; then
  sips -z 512 512 "$SOFTWARE/mac/AppIcon.png" --out "$ICONSET/icon_512x512.png" >/dev/null
  sips -z 1024 1024 "$SOFTWARE/mac/AppIcon.png" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
else
  /usr/bin/python3 "$SOFTWARE/mac/make-icon.py" "$ICONSET/icon_512x512.png"
fi
for s in 16 32 128 256; do sips -z $s $s "$ICONSET/icon_512x512.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null; done
cp "$ICONSET/icon_32x32.png" "$ICONSET/icon_16x16@2x.png"
cp "$ICONSET/icon_256x256.png" "$ICONSET/icon_128x128@2x.png"
cp "$ICONSET/icon_512x512.png" "$ICONSET/icon_256x256@2x.png"
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"
echo "assembled resources into $RES"
