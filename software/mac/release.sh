#!/bin/zsh
# A Developer ID release through Xcode's signed-in Apple account, with no clicks:
#   archive  -> Xcode signs with the account (and creates the Developer ID certificate the first time)
#   export   -> a Developer ID signed, hardened-runtime "PC Stats Panel.app" in build/export
#   notarize -> either Xcode's own upload to the notary service (--upload) or a notarytool profile (--notarize NAME)
#   staple, then package.sh wraps it into dist/PC-Stats-Panel-<version>.dmg
#
#   zsh mac/release.sh [--version 2.3] [--upload | --notarize pcstats] [--no-dmg]
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
X="$SRC/mac/xcode"
BUILD="$SRC/build"
VERSION="$(grep -o 'MARKETING_VERSION = [^;]*' "$X/PC Stats Panel.xcodeproj/project.pbxproj" | head -1 | sed 's/.*= //')"
MODE=""; PROFILE=""; DMG=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --upload) MODE="upload"; shift ;;
    --notarize) MODE="profile"; PROFILE="$2"; shift 2 ;;
    --no-dmg) DMG=0; shift ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
ARCHIVE="$BUILD/PC Stats Panel.xcarchive"
EXPORT="$BUILD/export"
APP="$EXPORT/PC Stats Panel.app"
rm -rf "$ARCHIVE" "$EXPORT"

echo "== 1/4 archive (version $VERSION, team HL52382HY3)"
xcodebuild -project "$X/PC Stats Panel.xcodeproj" -scheme "PC Stats Panel" -configuration Release \
  -archivePath "$ARCHIVE" MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$VERSION" \
  -allowProvisioningUpdates archive -quiet

echo "== 2/4 export for Developer ID"
xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportOptionsPlist "$X/ExportOptions.plist" \
  -exportPath "$EXPORT" -allowProvisioningUpdates -quiet
codesign -dv --verbose=2 "$APP" 2>&1 | grep -E "^Authority=Developer ID Application" | head -1 | sed 's/^/   /'

if [[ "$MODE" == "upload" ]]; then
  echo "== 3/4 notarize through Xcode's account"
  xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportOptionsPlist "$X/ExportOptions-upload.plist" \
    -exportPath "$BUILD/upload" -allowProvisioningUpdates -quiet
  echo "   uploaded; waiting for the ticket"
  for i in {1..40}; do
    if xcrun stapler staple "$APP" >/dev/null 2>&1; then echo "   stapled after ~$((i / 2)) min"; break; fi
    /usr/bin/python3 -c "import time; time.sleep(30)"
    [[ $i -eq 40 ]] && { echo "   no ticket after 20 min; run: xcrun stapler staple \"$APP\" later"; }
  done
elif [[ "$MODE" == "profile" ]]; then
  echo "== 3/4 notarize with notarytool profile $PROFILE"
  ZIP="$BUILD/PC-Stats-Panel-$VERSION.zip"
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
  xcrun stapler staple "$APP"
else
  echo "== 3/4 notarize: skipped (no --upload / --notarize)"
fi

if [[ "$DMG" == "1" ]]; then
  echo "== 4/4 disk image"
  zsh "$SRC/mac/package.sh" --app "$APP" --version "$VERSION" ${PROFILE:+--notarize "$PROFILE"}
else
  echo "== 4/4 dmg skipped"; echo "   app: $APP"
fi
