#!/bin/zsh
# Publishes the Homebrew cask for dist/PC-Stats-Panel-<version>.dmg to your tap repo (github.com/<you>/homebrew-tap),
# creating the tap the first time. Afterwards anyone can:  brew install --cask <you>/tap/pc-stats-panel
#
#   zsh mac/tap.sh [--version 2.3] [--tap-dir /path/to/homebrew-tap]
#
# Run it after mac/release.sh and `gh release create v<version> dist/PC-Stats-Panel-<version>.dmg`.
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(grep -o 'MARKETING_VERSION = [^;]*' "$SRC/mac/xcode/PC Stats Panel.xcodeproj/project.pbxproj" | head -1 | sed 's/.*= //')"
TAP_DIR="$SRC/../../homebrew-tap"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --tap-dir) TAP_DIR="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
OWNER="$(gh repo view --json owner -q .owner.login 2>/dev/null || true)"
[[ -n "$OWNER" ]] || { echo "gh cannot tell the GitHub owner; run inside the project repo with gh logged in"; exit 1; }
REPO="$(gh repo view --json name -q .name)"
DMG="$SRC/dist/PC-Stats-Panel-$VERSION.dmg"
[[ -f "$DMG" ]] || { echo "no $DMG (run mac/release.sh first)"; exit 1; }
SHA="$(shasum -a 256 "$DMG" | cut -c1-64)"

echo "== tap repo $OWNER/homebrew-tap in $TAP_DIR"
if [[ ! -d "$TAP_DIR/.git" ]]; then
  if gh repo view "$OWNER/homebrew-tap" >/dev/null 2>&1; then
    git clone -q "https://github.com/$OWNER/homebrew-tap.git" "$TAP_DIR"
  else
    mkdir -p "$TAP_DIR" && git -C "$TAP_DIR" init -q -b main
  fi
fi
mkdir -p "$TAP_DIR/Casks"
cat > "$TAP_DIR/Casks/pc-stats-panel.rb" <<EOF
cask "pc-stats-panel" do
  version "$VERSION"
  sha256 "$SHA"

  url "https://github.com/$OWNER/$REPO/releases/download/v#{version}/PC-Stats-Panel-#{version}.dmg"
  name "PC Stats Panel"
  desc "Stats dock for a Magedok T101F touch panel: live Mac stats, touch buttons and feeds"
  homepage "https://github.com/$OWNER/$REPO"

  livecheck do
    url :url
    strategy :github_latest
  end

  depends_on macos: ">= :ventura"

  app "PC Stats Panel.app"

  uninstall script:    {
              executable:   "#{appdir}/PC Stats Panel.app/Contents/MacOS/PCStatsPanel",
              args:         ["--uninstall"],
              must_succeed: false,
            },
            launchctl: "com.pcstatsdock.agent"

  zap trash: [
    "~/Applications/Stats Dock Admin.app",
    "~/Library/Application Support/pc-stats-dock",
    "~/Library/Caches/pc-stats-dock",
    "~/Library/Logs/pc-stats-dock",
  ]

  caveats <<~CAVEATS
    Open "PC Stats Panel" once: it installs itself as a login item and quits.
    Plug in the panel: the dashboard opens on it. Then switch on "PC Stats Panel" under
    System Settings > Privacy & Security > Accessibility (key buttons and touch need it).

    Optional:  brew install macmon                                   (temperatures and power)
               brew install jakehilborn/jakehilborn/displayplacer    (automatic screen arrangement)
  CAVEATS
end
EOF
cat > "$TAP_DIR/README.md" <<EOF
# homebrew-tap

\`\`\`
brew install --cask $OWNER/tap/pc-stats-panel
\`\`\`

PC Stats Panel turns a Magedok T101F touch panel into a wired stats dock for your Mac. Source and releases:
https://github.com/$OWNER/$REPO
EOF
git -C "$TAP_DIR" add -A
git -C "$TAP_DIR" -c commit.gpgsign=false commit -q -m "pc-stats-panel $VERSION" || true
if git -C "$TAP_DIR" remote get-url origin >/dev/null 2>&1; then
  git -C "$TAP_DIR" push -q origin main
else
  gh repo create "$OWNER/homebrew-tap" --public --source="$TAP_DIR" --remote=origin --push --description "Homebrew tap: PC Stats Panel" >/dev/null
fi
echo "   cask: $TAP_DIR/Casks/pc-stats-panel.rb  (version $VERSION, sha256 ${SHA:0:12}...)"
echo "   install with:  brew install --cask $OWNER/tap/pc-stats-panel"
