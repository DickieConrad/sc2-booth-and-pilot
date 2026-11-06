# Stop what live/start.ps1 started: only the PIDs in live/out/stack.pids (and their child processes), nothing
# else on this machine. Services started some other way (e.g. from a Claude Code session) are left alone.
#   powershell -ExecutionPolicy Bypass -File live\stop.ps1                  # the whole stack
#   powershell -ExecutionPolicy Bypass -File live\stop.ps1 -Only mic,brain  # just these (see live\restart.ps1)
param([string[]]$Only)
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "live\out\stack.pids"
if (-not (Test-Path $pidFile)) { Write-Host "no live/out/stack.pids: nothing started by start.ps1"; exit 0 }
$keep = @()
foreach ($line in Get-Content $pidFile) {
    $name, $id = $line -split "=", 2
    if (-not $id) { continue }
    if ($Only -and $Only -notcontains $name) { $keep += $line; continue }
    if (Get-Process -Id ([int]$id) -ErrorAction SilentlyContinue) {
        taskkill /PID $id /T /F | Out-Null
        Write-Host "stopped $name (pid $id)"
    } else {
        Write-Host "$name (pid $id) already gone"
    }
}
if ($keep) { $keep | Set-Content -Encoding ascii $pidFile } else { Remove-Item $pidFile }
