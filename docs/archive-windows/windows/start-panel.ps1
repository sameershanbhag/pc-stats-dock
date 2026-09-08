# Starts the agent, waits until it answers, then opens the dashboard full-screen on the small display.
$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$cfg = Get-Content (Join-Path $Root "agent\config.json") -Raw | ConvertFrom-Json
$port = if ($cfg.port) { $cfg.port } else { 4400 }

$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" | Where-Object { $_.CommandLine -like "*agent.py*" }
if (-not $running) {
  Start-Process "pythonw.exe" -ArgumentList "`"$(Join-Path $Root 'agent\agent.py')`"" -WorkingDirectory (Join-Path $Root "agent") -WindowStyle Hidden
}
for ($i = 0; $i -lt 60; $i++) {
  try { Invoke-RestMethod "http://localhost:$port/api/health" -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
}
& (Join-Path $PSScriptRoot "launch-kiosk.ps1") -Url "http://localhost:$port/"
