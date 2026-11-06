# Restart pieces of the live coach stack cleanly: stop them by their PIDs from live/out/stack.pids (live\stop.ps1
# -Only), start them again with live\start.ps1 -NoClaude (which skips everything still running), then wait until
# each one is really up: helper/panel-reader/coach-core listening on their port, the mic's "listening on" line, the
# brain's "BRAIN up". Never restart the helper mid-game (a game splits into two DB rows): it refuses unless -Force.
#   powershell -ExecutionPolicy Bypass -File live\restart.ps1 mic
#   powershell -ExecutionPolicy Bypass -File live\restart.ps1 helper,brain
#   powershell -ExecutionPolicy Bypass -File live\restart.ps1            # everything
param([string[]]$Names = @("coach-core", "panel-reader", "helper", "brain", "mic"), [switch]$Force)
$ErrorActionPreference = "Stop"
$live = $PSScriptRoot
$out = Join-Path $live "out"
$known = "coach-core", "panel-reader", "helper", "brain", "mic"
$Names = @($Names | ForEach-Object { $_ -split "," } | Where-Object { $_ })
foreach ($n in $Names) { if ($known -notcontains $n) { throw "unknown service '$n' (one of: $($known -join ', '))" } }

if ($Names -contains "helper" -and -not $Force) {
    try {
        $s = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/state -TimeoutSec 3).Content | ConvertFrom-Json
        if ($s.in_game) { throw "a game is on: restarting the helper now splits it in the DB (use -Force to do it anyway)" }
    } catch [System.Net.WebException] { }   # helper down: nothing to protect
}

$ports = @{ "helper" = 8765; "panel-reader" = 8766; "coach-core" = 8767 }
$logs = @{ "mic" = @("mic-run.log", "listening on"); "brain" = @("brain-run.log", "BRAIN up") }
$marks = @{}
foreach ($n in $logs.Keys) {
    $f = Join-Path $out $logs[$n][0]
    $marks[$n] = if (Test-Path $f) { (Get-Content $f).Count } else { 0 }
}

& (Join-Path $live "stop.ps1") -Only $Names
# a helper started outside start.ps1 (no PID on file) still holds :8765: stop that listener too, by PID, after
# checking it is our timing.py. Only the helper: on :8766/:8767 the Windows listener can be WSL's shared relay.
if ($Names -contains "helper") {
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"
        if ($p -and $p.CommandLine -match "timing\.py") {
            taskkill /PID $_.OwningProcess /T /F | Out-Null
            Write-Host "stopped the old helper on :8765 (pid $($_.OwningProcess))"
        }
    }
}
Start-Sleep -Seconds 1
& (Join-Path $live "start.ps1") -NoClaude | Out-Null

$deadline = (Get-Date).AddSeconds(150)   # the mic's medium.en takes about a minute to load
$waiting = [System.Collections.ArrayList]@($Names)
while ($waiting.Count -and (Get-Date) -lt $deadline) {
    foreach ($n in @($waiting)) {
        $up = $false
        if ($ports.ContainsKey($n)) {
            $up = @(Get-NetTCPConnection -LocalPort $ports[$n] -State Listen -ErrorAction SilentlyContinue).Count -eq 1
        } else {
            $f = Join-Path $out $logs[$n][0]
            $new = @(Get-Content $f -ErrorAction SilentlyContinue | Select-Object -Skip $marks[$n])
            $up = [bool]($new | Select-String -SimpleMatch $logs[$n][1])
        }
        if ($up) { Write-Host "$n up"; $waiting.Remove($n) }
    }
    if ($waiting.Count) { Start-Sleep -Seconds 2 }
}
if ($waiting.Count) { Write-Host "NOT up after 150 s: $($waiting -join ', ') (see live/out logs)"; exit 1 }
