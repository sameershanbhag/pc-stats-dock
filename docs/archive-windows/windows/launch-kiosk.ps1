param(
  [int]$Width = 1540,
  [int]$Height = 720,
  [string]$Url = "http://localhost:4400/"
)
# Finds the panel by its resolution (falls back to the smallest display) and pins an Edge kiosk window to it.
Add-Type -AssemblyName System.Windows.Forms
$screens = [System.Windows.Forms.Screen]::AllScreens
$s = $screens | Where-Object { $_.Bounds.Width -eq $Width -and $_.Bounds.Height -eq $Height } | Select-Object -First 1
if (-not $s) { $s = $screens | Sort-Object { $_.Bounds.Width * $_.Bounds.Height } | Select-Object -First 1 }

$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
$profile = Join-Path $env:LOCALAPPDATA "pc-stats-panel\edge"
New-Item -ItemType Directory -Force -Path $profile | Out-Null

Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*pc-stats-panel*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Process $edge -ArgumentList @(
  "--kiosk", $Url, "--edge-kiosk-type=fullscreen",
  "--window-position=$($s.Bounds.X),$($s.Bounds.Y)", "--window-size=$($s.Bounds.Width),$($s.Bounds.Height)",
  "--user-data-dir=`"$profile`"", "--no-first-run", "--disable-features=TranslateUI,msEdgeSidebarV2", "--autoplay-policy=no-user-gesture-required"
)
