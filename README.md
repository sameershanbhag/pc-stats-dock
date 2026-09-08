# PC Stats Touch Display — the simple plan (Mac)

One purchase. One cable. One script.

## 1. Buy one thing

**Magedok T101F** — 10.1" wide touchscreen (1540×720), USB-C + HDMI, fold-out stand, cables and
power adapter in the box. Ordered on Amazon 2026-09-06 (B0FGCVT6P2).

## 2. Plug it in

One USB-C cable (included) from the Mac to the monitor's full-function USB-C port. Video, touch
and power in one. macOS shows a second display immediately.

For a laptop you may want the included power adapter in the monitor's power port too, so the
panel does not draw from the MacBook's battery.

## 3. Run one script

```
zsh ~/Desktop/Projects/HardwareProjects/pc-stats-dock/software/mac/install.sh
```
It installs one Homebrew tool (macmon, for temperatures and power on Apple Silicon), starts the
agent at login, and opens the Accessibility pane for the one permission key-combination buttons
need. From then on: plug the panel in, the dashboard opens on it by itself; unplug it, the window
closes.

- Dashboard: CPU / GPU load and temperature, memory, system power, fan, disk, network, a two-minute
  load graph, and twelve buttons.
- Buttons editor: **http://localhost:4400/admin** — drag to reorder, pick from a library
  (Spaces, Mission Control, Spotlight, screenshots, Spotify, lock, apps, volume, mic…), test, save.
- Details: `software/README.md`.

## Touch and the display, handled for you

macOS aims a USB touchscreen at the **main** display and likes to make a freshly plugged-in panel
the main display. The agent keeps the big monitor as main, parks the panel next to it, moves stray
windows back, and maps the panel's touches onto the panel (same Accessibility permission as the
buttons). Single-finger taps and drags work; multi-touch gestures do not. A tap first makes the app under
  your finger the active one (a Mac app ignores the first click on an inactive window), and when the
  finger lifts the pointer returns to where it was on your big screen.

## Installing without a Terminal

`software/mac/package.sh` produces a `.dmg` with a self-contained *PC Stats Panel* app: drag it to Applications,
open it once, plug in the panel. See `software/README.md` for signing and notarizing it with an Apple Developer
account so other Macs open it without warnings.

## Admin page

*Stats Dock Admin* in the Dock (or the ⚙ on the panel) opens http://localhost:4400/admin, where you set
what the buttons do, the feeds, and where the panel sits.

## Later, only if you want

- **Macro pad with a knob** ($55, Adafruit MacroPad): physical keys, no software needed. Plugs
  into one of the monitor's own USB-A ports.
- **3D-printed stand or dock.** The fold-out stand works until then.
- **LED underglow.**

Detailed research, parts and prices are archived in `docs/`. The Windows version of the software
is in `docs/archive-windows/` in case a PC ever joins the setup.

## Budget

$99–129 spent. Nothing else required.
