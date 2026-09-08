#Requires -RunAsAdministrator
<#
  PC Stats Panel — one-time install on the gaming PC.
  Run in an elevated PowerShell:   powershell -ExecutionPolicy Bypass -File .\install.ps1

  What it does:
   1. Installs Python 3.12 and LibreHardwareMonitor with winget (skips what is already there).
   2. Installs the two Python packages the agent needs for actions (pyautogui, pycaw).
   3. Registers two logon tasks: "PC Stats Panel - Sensors" (LibreHardwareMonitor, elevated)
      and "PC Stats Panel" (agent + full-screen dashboard on the small display).
   4. Prints the two clicks you still have to do inside LibreHardwareMonitor once.
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # ...\software
$Agent = Join-Path $Root "agent\agent.py"

function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "== 1/4 Python" -ForegroundColor Cyan
if (-not (Have "python")) { winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements }
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
python --version

Write-Host "== 2/4 LibreHardwareMonitor" -ForegroundColor Cyan
winget install -e --id LibreHardwareMonitor.LibreHardwareMonitor --silent --accept-package-agreements --accept-source-agreements 2>$null | Out-Null
$lhm = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "LibreHardwareMonitor.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if (-not $lhm) { $lhm = Get-ChildItem "$env:ProgramFiles", "${env:ProgramFiles(x86)}", "$env:LOCALAPPDATA\Programs" -Recurse -Filter "LibreHardwareMonitor.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName }
if (-not $lhm) { throw "LibreHardwareMonitor.exe not found. Download it from github.com/LibreHardwareMonitor and re-run." }
Write-Host "   found $lhm"

Write-Host "== 3/4 Python packages" -ForegroundColor Cyan
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pyautogui pycaw comtypes

Write-Host "== 4/4 Logon tasks" -ForegroundColor Cyan
$user = "$env:USERDOMAIN\$env:USERNAME"
# Sensors: LibreHardwareMonitor needs admin for CPU/GPU temperatures.
$a1 = New-ScheduledTaskAction -Execute $lhm
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $user
$s1 = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "PC Stats Panel - Sensors" -Action $a1 -Trigger $t1 -Settings $s1 -RunLevel Highest -User $user -Force | Out-Null
# Panel: agent + kiosk, normal privileges, 20 s after logon so the displays are up.
$start = Join-Path $PSScriptRoot "start-panel.ps1"
$a2 = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$start`""
$t2 = New-ScheduledTaskTrigger -AtLogOn -User $user
$t2.Delay = "PT20S"
Register-ScheduledTask -TaskName "PC Stats Panel" -Action $a2 -Trigger $t2 -Settings $s1 -User $user -Force | Out-Null

Write-Host ""
Write-Host "Installed. Two clicks left, once, inside LibreHardwareMonitor:" -ForegroundColor Green
Write-Host "  1. Options -> Remote Web Server -> Run   (port 8085; allow it on Private networks if Windows asks)"
Write-Host "  2. Options -> Minimize To Tray  (and Start Minimized)"
Write-Host "Then log out and back in, or run:  powershell -ExecutionPolicy Bypass -File `"$start`""
Start-Process $lhm
