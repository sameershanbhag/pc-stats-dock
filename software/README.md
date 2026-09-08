# PC Stats Panel — macOS software

One folder, one script, nothing else to install by hand.

## Install (once)

```
zsh ~/Desktop/Projects/HardwareProjects/pc-stats-dock/software/mac/install.sh
```
It installs two small Homebrew tools (`macmon` for temperatures and power, `m1ddc` for monitor
volume over DDC), creates a tiny wrapper app named **PC Stats Panel** in `~/Applications` so
macOS has a stable name to grant permissions to, registers it to start at login, and triggers
macOS's own Accessibility dialog.

**The one permission:** System Settings › Privacy & Security › Accessibility → switch on
“PC Stats Panel”. Key-combination buttons need it; nothing else does. Until it is on, the
dashboard shows a banner and key buttons say “needs permission”; tapping one opens the pane.

Then plug the panel into the Mac with its USB-C cable. The dashboard opens full-screen on it by
itself, and closes when you unplug it. The buttons editor is at **http://localhost:4400/admin**.

## What the buttons can do

| type | field | does |
|---|---|---|
| Key combination | Cmd / Ctrl / Option / Shift + a key | e.g. Ctrl+← / Ctrl+→ to switch Spaces, Ctrl+↑ Mission Control, Cmd+Space Spotlight |
| Open app | app name | `open -a`, e.g. Safari, Slack, Spotify |
| Volume | up / down / mute | speakers |
| Mic | — | mutes the microphone (input volume 0) and restores it; tile turns red while muted |
| Shortcut | shortcut name | runs anything you built in the Shortcuts app (Focus modes, HomeKit scenes…) |
| AppleScript | script | e.g. `tell application "Spotify" to playpause` |
| Shell | command | e.g. `screencapture -i -c` |

Everything except key combinations works with no permissions. Key combinations are posted by
the **launcher** (the app's own compiled executable, `mac/launcher.c`), which the Python agent
asks over a pipe. macOS grants Accessibility to executables, and that one is “PC Stats Panel”.

macOS answers the permission question from a per-process cache, so a grant made while the
agent is running is invisible to it. The launcher therefore re-checks in a fresh copy of itself
every few seconds while untrusted, and the agent restarts itself the moment the grant appears
(the dashboard window stays open). You never need to restart anything by hand.

If the launcher is ever rebuilt (the installer only does that when `launcher.c` changes), macOS
treats it as a new app: the installer clears the old entry and macOS asks again; switch
“PC Stats Panel” on once more. Updates to the Python and HTML files never require that.

**Volume on a monitor:** if sound goes to a display over DisplayPort or HDMI, macOS has no
system volume for it. The agent then tries the monitor's DDC volume (`m1ddc`); if the monitor
does not answer DDC either (the Samsung Odyssey G95SC does not), the volume tiles say so and the
library offers per-app volume buttons for Music and Spotify, which work on any output.

## AI chats: know when a chat has returned, jump straight to it

Every terminal, IDE and desktop AI you use can report to the panel when a turn finishes or
when it is waiting for you. The "AI chats" feed lists them newest first with the project and the
app; tapping one jumps to that exact place:

| Where the chat runs | Jump goes to |
|---|---|
| Terminal.app | the tab (found by its tty) |
| iTerm2 | the session (found by its id) |
| VS Code, Cursor, Windsurf | the window for that project folder |
| Ghostty, Warp, kitty, others | the app, and the window whose title mentions the project if there is one |
| inside tmux | the pane, then the terminal app |

**Setup, once:** admin page → Right side → the AI chats feed → *Install hooks*. It adds a Stop
and a Notification hook to Claude Code's `~/.claude/settings.json`, a `notify` program to Codex's
`~/.codex/config.toml`, and a `stop` hook to Cursor's `~/.cursor/hooks.json` where those exist.
Nothing else in those files is touched; *Uninstall* reverses it. First jump into each app asks
for the Automation permission ("PC Stats Panel wants to control Terminal"): click OK once.

Any other tool or script can report too:
```
~/Library/Application\ Support/pc-stats-dock/app/agent/hooks/notify.py --tool Gemini --title Finished --text "3 files changed"
```
or `POST http://127.0.0.1:4400/api/events` with `{"tool","title","text","state","cwd"}`.

Entries: green edge = finished, amber = needs you (a permission prompt or a question), dim =
already seen. Tapping marks it seen. One entry per chat session, so a busy session does not
flood the list.

## Right side: message feeds

The right column can show two configurable feeds instead of (or next to) the buttons. Set it
up at **http://localhost:4400/admin → Right side**. Layouts: two feeds, one feed plus six
buttons, or twelve buttons. Each feed has a title, a source and a message count.

| Source | Setup | What it shows |
|---|---|---|
| **Slack** | Create a small app at api.slack.com/apps → *From scratch* → OAuth & Permissions → add **User Token Scopes** `channels:history`, `groups:history`, `im:history`, `mpim:history`, `channels:read`, `groups:read`, `im:read`, `mpim:read`, `users:read` → *Install to Workspace* → copy the `xoxp-…` token into the admin page. Some workspaces require an admin to approve the app. | Latest messages in the channels you list; `dm` means your direct messages. Tap a message to open it in Slack. |
| **Microsoft Teams** | Needs an app registration in your Microsoft 365 tenant (Azure portal → App registrations → New: *Accounts in this org*, redirect `https://login.microsoftonline.com/common/oauth2/nativeclient`, Authentication → *Allow public client flows: Yes*, API permissions → delegated `Chat.Read`, `User.Read`). Paste the client id, click *Sign in*, enter the code shown. Corporate tenants often need an admin to consent. | The latest message of each chat, newest first. Tap to open in Teams. |
| **Notification Center** | No accounts, no tokens. Tick the apps (Slack, Teams, Messages, Mail, Discord…), then give **PC Stats Panel** Full Disk Access: System Settings › Privacy & Security › Full Disk Access (the admin page has a button that opens it). | Whatever macOS notified you about from those apps: mentions, DMs, channels with alerts on. The simplest way to get Teams and Slack on the panel side by side. |

Tokens are stored in `~/Library/Application Support/pc-stats-dock/config.json` (Slack) and
`tokens.json` (Microsoft, file mode 600). They never leave the Mac and are never sent to the
pages; the admin page only shows whether one is stored.

## Touch and the display arrangement

macOS treats a USB touchscreen as a mouse aimed at the **main** display, and on first plug-in it
often makes the new panel the main display, which drags your menu bar, Dock and new windows onto
it. The agent handles both, automatically, every time the panel is connected:

- **The big monitor stays main.** The panel is parked above it (change to below/left/right in the
  admin page's *Display* card) using `displayplacer`, and windows that landed on the panel are moved
  back to the main display. Only the dashboard lives on the panel.
- **Touch is mapped onto the panel.** The launcher's `--touch-map` mode watches for the panel's
  touchscreen (HID usage "touch screen") and, through an event tap, relocates its taps from the main
  display onto the panel. It uses the same Accessibility permission as the key buttons; the admin
  page shows its state (*Touch: mapped onto the panel*), and `~/Library/Logs/pc-stats-dock/touch.log`
  has the details. Single-finger taps and drags work; multi-touch gestures do not. A tap first makes the app under
  your finger the active one (a Mac app ignores the first click on an inactive window), and when the
  finger lifts the pointer returns to where it was on your big screen.

If you would rather not have any of this, untick *keep the panel for the dock only* in the admin page.

## Getting to the admin page

- **Dock:** the installer puts *Stats Dock Admin* in your Mac's Dock; click it and the admin page opens in
  your browser (`mac/dock-admin.sh --remove` takes it out again).
- **Panel:** the ⚙ button at the top right of the dashboard opens the same page on your Mac.

## Key buttons and macOS shortcuts

Key presses are posted as real keyboard events with the flag bits a physical keyboard sets (arrows and
F keys carry the fn bit), which macOS's own shortcuts such as Spaces, Mission Control and Show Desktop
require. A tap on a key button waits for your finger to lift (macOS ignores shortcuts while a button is
held), moves the mouse pointer back to your big screen, then presses the key: macOS aims Spaces and Spotlight
at the display under the pointer, not at the panel the tap just activated. Right after the key, keyboard
focus goes back to the app you were using; for Spotlight that happens first, since Spotlight closes when
focus moves. The dashboard window comes back on its own whenever it is missing while the panel is plugged in.

## Install from the app (no Terminal)

`zsh mac/package.sh` builds a self-contained `PC Stats Panel.app` and `dist/PC-Stats-Panel-<version>.dmg`.
Whoever gets the .dmg drags the app to Applications and opens it once: it installs itself as a login item
(the agent, the dashboard and the touch mapper all live inside the app), adds the *Stats Dock Admin* icon to
the Dock and shows what is left to do (the Accessibility switch). Homebrew's `macmon` is optional
(temperatures and power); `displayplacer` is optional (the automatic screen arrangement). To remove it:

```
"/Applications/PC Stats Panel.app/Contents/MacOS/PCStatsPanel" --uninstall
```

For a build that other Macs open without warnings (Developer ID + notarization), use the Xcode route. It needs
Xcode with your Apple Developer account signed in (Xcode › Settings › Accounts) and nothing else: Xcode creates
the Developer ID certificate itself, and notarizes through the same account.

```
zsh mac/release.sh --upload            # archive, export for Developer ID, notarize, staple, dist/*.dmg
zsh mac/release.sh --notarize pcstats  # same, but notarizing with a notarytool keychain profile
```

`mac/xcode/` is the Xcode project behind it (the launcher target plus a build phase that puts the agent, the
dashboard and the icon into the bundle); `mac/package.sh --app <app>` wraps an exported app into the .dmg.

Icons: drop your own 1024×1024 PNGs (transparent outside the rounded square) at `mac/AppIcon.png` (the app)
and `mac/AdminIcon.png` (the Dock shortcut); `swift mac/icon-from-image.swift picture.jpg mac/AppIcon.png`
cuts one out of a generated picture. Without them a simple gauge icon is drawn.

If you have a local "Developer ID Application" identity instead, `package.sh --identity "..." --notarize pcstats`
signs without Xcode, and `PCSTATS_IDENTITY="..." zsh mac/install.sh` keeps the Accessibility permission across
rebuilds of the developer install.

## Try it without the panel

```
python3 software/agent/agent.py --dry-run --no-kiosk
```
Open http://localhost:4400 for the dashboard with your Mac's real sensors, and /admin for the
editor. In dry-run mode buttons only log what they would do.

## Tests

```
cd software
python3 -m unittest tests.test_agent tests.test_feeds tests.test_events   # agent, feeds, AI events, hooks
node tests/test_pages.mjs                         # dashboard + admin pages (needs `npm i jsdom` somewhere on NODE_PATH)
DRY_RUN=1 zsh mac/install.sh                      # prints the LaunchAgent it would install, changes nothing
```
Everything that touches the system (osascript, open, shortcuts, macmon, Chrome) is mocked in the tests;
`python3 agent/agent.py --dry-run` is the way to try the real thing without pressing any keys.

## Where things live after install

- Code the agent runs: `~/Library/Application Support/pc-stats-dock/app/` (a copy; re-run the installer after editing the source here). It is deliberately outside Desktop, Documents and Downloads, which macOS guards with extra prompts.
- Your buttons: `~/Library/Application Support/pc-stats-dock/config.json`. Updates never touch it.
- Wrapper app: `~/Applications/PC Stats Panel.app` (a small compiled launcher and key service, ad-hoc signed). This is the name macOS shows in Privacy & Security.
- Logs: `~/Library/Logs/pc-stats-dock/`.

## Files

`agent/agent.py` web server + actions · `agent/events.py` + `agent/focus.py` AI-chat events and jump-to-window · `agent/hooks/` the hook scripts and their installer · `agent/feeds.py` Slack / Teams / Notification Center feeds · `agent/sensors_mac.py` macmon and built-in sensors ·
`agent/displays.py` finds the panel and runs the Chrome kiosk window · `agent/config.json` your
buttons · `dashboard/` the pages · `mac/install.sh`, `mac/uninstall.sh`. Logs:
`~/Library/Logs/pc-stats-dock/`. Only `localhost` can reach the agent.
