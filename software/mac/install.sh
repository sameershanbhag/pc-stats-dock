#!/bin/zsh
# PC Stats Panel — one-time install on the Mac. Run:  zsh install.sh      (DRY_RUN=1 to only print what it would do)
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"          # this folder (the source you edit)
DIR="$HOME/Library/Application Support/pc-stats-dock/app"   # where the agent runs from: not inside Desktop/Documents/Downloads,
                                                            # which macOS guards with extra permission prompts
APP="$HOME/Applications/PC Stats Panel.app"     # tiny wrapper app: gives the agent a stable identity for the Accessibility permission
PLIST="$HOME/Library/LaunchAgents/com.pcstatsdock.agent.plist"
LOGS="$HOME/Library/Logs/pc-stats-dock"
LABEL="com.pcstatsdock.agent"
PY="$(command -v python3)"
IDENTITY="${PCSTATS_IDENTITY:--}"   # a real signing identity ("Developer ID Application: ...") keeps the Accessibility grant across rebuilds
DRY="${DRY_RUN:-0}"

echo "== 1/6 Homebrew tools: macmon (temperatures/power) and m1ddc (monitor volume over DDC)"
if [[ "$DRY" == "1" ]]; then
  echo "   (dry run) would run: brew install macmon m1ddc"
elif command -v brew >/dev/null 2>&1; then
  brew list macmon >/dev/null 2>&1 || brew install macmon
  brew list m1ddc  >/dev/null 2>&1 || brew install m1ddc || echo "   (m1ddc is optional, continuing)"
else
  echo "   Homebrew not found. Install it from https://brew.sh, then re-run. (The panel works without it, just no temperatures.)"
fi

echo "== 2/6 copy the agent to: $DIR"
if [[ "$DRY" == "1" ]]; then
  echo "   (dry run) would rsync $SRC/{agent,dashboard} there (your buttons stay in $HOME/Library/Application Support/pc-stats-dock/config.json)"
else
  mkdir -p "$DIR"
  rsync -a --delete --exclude '__pycache__' --exclude 'config.json' "$SRC/agent/" "$DIR/agent/"
  rsync -a --delete "$SRC/dashboard/" "$DIR/dashboard/"
  cp "$SRC/agent/config.json" "$DIR/agent/config.json"     # shipped defaults; the live config is seeded from it once
fi

echo "== 3/6 wrapper app: $APP"
# A compiled launcher (not a shell script) so macOS shows "PC Stats Panel" in Privacy & Security.
LAUNCHER_SRC="$SRC/mac/launcher.c"
INFO='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PC Stats Panel</string>
  <key>CFBundleDisplayName</key><string>PC Stats Panel</string>
  <key>CFBundleIdentifier</key><string>com.pcstatsdock.agent</string>
  <key>CFBundleExecutable</key><string>PCStatsPanel</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>2.3</string>
  <key>CFBundleShortVersionString</key><string>2.3</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>'
if [[ "$DRY" == "1" ]]; then
  echo "   (dry run) would compile $LAUNCHER_SRC into $APP/Contents/MacOS/PCStatsPanel and ad-hoc sign the bundle"
else
  STAMP="$HOME/Library/Application Support/pc-stats-dock/launcher.stamp"
  WANT="$(shasum -a 256 "$LAUNCHER_SRC" | cut -c1-16)-$PY-$DIR-$IDENTITY"
  if [[ -x "$APP/Contents/MacOS/PCStatsPanel" && -f "$STAMP" && "$(cat "$STAMP")" == "$WANT" ]]; then
    echo "   launcher unchanged, keeping it (rebuilding would reset its Accessibility permission)"
    SKIP_BUILD=1
  fi
  if [[ -z "$SKIP_BUILD" ]]; then
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS"
  print -r -- "$INFO" > "$APP/Contents/Info.plist"
  if command -v clang >/dev/null 2>&1 && clang -O2 -Wall -framework ApplicationServices -framework CoreFoundation -framework IOKit \
       -DPYTHON="\"$PY\"" -DAGENT="\"$DIR/agent/agent.py\"" -DAGENT_DIR="\"$DIR/agent\"" \
       -o "$APP/Contents/MacOS/PCStatsPanel" "$LAUNCHER_SRC" 2>/tmp/pcstats-clang.log; then
    echo "   compiled launcher"
    mkdir -p "$(dirname "$STAMP")"; print -r -- "$WANT" > "$STAMP"
    REBUILT=1
  else
    echo "   no C compiler (install Xcode Command Line Tools: xcode-select --install); using an AppleScript applet instead"
    rm -rf "$APP"
    osacompile -o "$APP" -e "do shell script \"exec '$PY' '$DIR/agent/agent.py' >/dev/null 2>&1\""
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleName 'PC Stats Panel'" "$APP/Contents/Info.plist" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.pcstatsdock.agent" "$APP/Contents/Info.plist" 2>/dev/null || true
  fi
  codesign --force --sign "$IDENTITY" --identifier com.pcstatsdock.agent "$APP" 2>/dev/null && echo "   signed ($([[ "$IDENTITY" == "-" ]] && echo "ad hoc" || echo "$IDENTITY"))" || echo "   (could not sign; continuing)"
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
  fi
fi

echo "== 4/6 login item (LaunchAgent)"
PLIST_BODY="<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/$(defaults read "$APP/Contents/Info.plist" CFBundleExecutable 2>/dev/null || echo PCStatsPanel)</string></array>
  <key>WorkingDirectory</key><string>$DIR/agent</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>$LOGS/agent.log</string>
  <key>StandardErrorPath</key><string>$LOGS/agent.err.log</string>
</dict></plist>"
if [[ "$DRY" == "1" ]]; then
  echo "$PLIST_BODY"; echo "   (dry run) would load it with launchctl"; exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" "$LOGS"
print -r -- "$PLIST_BODY" > "$PLIST"
if [[ -n "$REBUILT" && "$IDENTITY" == "-" ]]; then   # ad-hoc identities change with every build; real ones do not
  # A rebuilt launcher is a new app to macOS; drop any stale Accessibility entry so the fresh prompt registers a matching one.
  tccutil reset Accessibility com.pcstatsdock.agent >/dev/null 2>&1 && echo "   cleared the old Accessibility entry (launcher changed)" || true
fi
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
pkill -f "software/agent/agent.py" 2>/dev/null || true
pkill -9 -f "user-data-dir=$HOME/Library/Application Support/pc-stats-dock/chrome" 2>/dev/null || true   # the old dashboard window, at once; the new agent opens a fresh one
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for i in {1..20}; do curl -fsS http://127.0.0.1:4400/api/health >/dev/null 2>&1 && break; sleep 1; done
HEALTH="$(curl -fsS http://127.0.0.1:4400/api/health 2>/dev/null || true)"
if [[ -n "$HEALTH" ]]; then echo "   agent is running: http://localhost:4400   buttons editor: http://localhost:4400/admin"; else echo "   agent did not answer within 20 s; check $LOGS/agent.err.log"; fi

echo "== 5/6 the one permission"
if [[ "$HEALTH" == *'"accessibility": true'* ]]; then
  echo "   Accessibility already granted. Key buttons and touch work."
elif [[ -n "$REBUILT" ]]; then
  echo "   The launcher was rebuilt, which macOS treats as a new app (key buttons and touch pause until this is done)."
  echo "   In System Settings › Privacy & Security › Accessibility:"
  echo "   if “PC Stats Panel” is listed and ON, switch it OFF and ON again; if it is missing, click “+”, press Cmd+Shift+G,"
  echo "   paste  $APP  and add it, then switch it on."
else
  echo "   In System Settings › Privacy & Security › Accessibility switch on “PC Stats Panel”."
  echo "   If it is not in the list yet: click “+”, press Cmd+Shift+G, paste  $APP  and add it, then switch it on."
  echo "   That is the only permission; volume, mic, apps and Shortcuts need none."
fi
echo
echo "Done. Plug the panel in: the dashboard opens on it by itself."

echo "== 6/6 Stats Dock Admin in the Dock (opens http://localhost:4400/admin)"
zsh "$SRC/mac/dock-admin.sh"
