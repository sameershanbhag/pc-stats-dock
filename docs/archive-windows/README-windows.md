# PC Stats Panel — software

One folder. Three steps on the gaming PC.

## Install (once)

1. Copy this `software` folder to the PC (for example `C:\pc-stats-panel\software`).
2. Right-click PowerShell → Run as administrator, then:
   ```
   powershell -ExecutionPolicy Bypass -File C:\pc-stats-panel\software\windows\install.ps1
   ```
   It installs Python and LibreHardwareMonitor (via winget), two Python packages, and two logon tasks.
3. In the LibreHardwareMonitor window that opens: **Options → Remote Web Server → Run**, and
   **Options → Minimize To Tray**. Done. Log out and back in, and the dashboard opens full-screen on the panel.

If the dashboard opens on the wrong screen, the panel's resolution is not 1540×720 as Windows
reports it. Run `windows\launch-kiosk.ps1 -Width <w> -Height <h>` with the numbers Windows shows
in Settings → Display, or leave it: it falls back to the smallest display.

## Buttons: the admin page

Open **http://localhost:4400/admin** in any browser on the PC. You get the 3×4 grid exactly as it
is on the panel: click a slot to edit its label, small text, glyph and what it does; drag slots to
swap them; pick ready-made actions from the library (desktops, screenshots, media keys, lock,
Task Manager, Discord mute, taskbar pins…); **Test now** fires the action immediately; **Save**
writes `agent\config.json` and the panel updates within a few seconds, no restart.

Key combinations: tick Win / Ctrl / Alt / Shift and type or press the key. Everything except the
Win key can be captured by pressing it in the Key box.

## Buttons: by hand

Edit `agent\config.json`. Twelve buttons, top-left to bottom-right. Types:

| type | fields | does |
|---|---|---|
| `hotkey` | `keys: ["win","ctrl","left"]` | presses a key combination (pyautogui names) |
| `launch` | `path: "C:\\...\\app.exe"` | opens a program or file |
| `volume` | `dir: "up" / "down" / "mute"` | media keys |
| `mic` | — | toggles the default microphone's mute; the button turns red while muted |
| `powershell` | `command: "..."` | runs a PowerShell one-liner |

The defaults switch virtual desktops, open Task View, snap layouts, launch taskbar pins 1–3,
mute the mic and change volume. Saving through the admin page applies immediately; a hand edit is picked up on the agent's next start
(`Task Scheduler → PC Stats Panel → Run`, or log out and in).

## Try it without the panel

```
python agent\agent.py --demo
```
then open http://localhost:4400 in any browser. Fake numbers, buttons only log. Opening
`dashboard\index.html` directly does the same without Python.

## How it works

`agent\agent.py` polls LibreHardwareMonitor's `http://localhost:8085/data.json` twice a second,
flattens it (`sensors.py`), serves `dashboard\index.html` and `dashboard\admin.html` (at `/admin`)
plus `/api/stats`, `/api/config`, `/api/health`, runs `POST /api/action/<id>` (and `POST /api/action`
with an inline action for the admin page's Test button), and saves `POST /api/admin/config`. `windows\start-panel.ps1` starts the agent and
`launch-kiosk.ps1` pins an Edge kiosk window to the panel. Nothing listens outside `localhost`.

## Office laptop

Plug the panel in with one USB-C cable. It is a second monitor with touch immediately. To get
the gauges and buttons there too, repeat the install on the laptop if you are allowed to.
