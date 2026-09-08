#!/bin/zsh
PLIST="$HOME/Library/LaunchAgents/com.pcstatsdock.agent.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
pkill -f "software/agent/agent.py" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$HOME/Applications/PC Stats Panel.app" "$HOME/Library/Application Support/pc-stats-dock"   # app copy, kiosk profile and your button config
zsh "$(dirname "$0")/dock-admin.sh" --remove 2>/dev/null || rm -rf "$HOME/Applications/Stats Dock Admin.app"
tccutil reset Accessibility com.pcstatsdock.agent >/dev/null 2>&1 || true
echo "Agent, login item and wrapper app removed. Homebrew tools stay (brew uninstall macmon m1ddc to remove them)."
echo "You can also remove “PC Stats Panel” from System Settings › Privacy & Security › Accessibility."
