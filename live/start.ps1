# Start the live coach stack, each piece in its own minimized window so it outlives the Claude Code session:
#   coach-core (:8767) and panel-reader (:8766) in WSL, the helper (:8765), the brain and the mic on Windows.
# A piece already running (its port listening, or its PID from the last start still alive) is skipped, never
# restarted: nothing else on this machine is touched. Logs append to live/out/*.log as before.
# PIDs go to live/out/stack.pids for live/stop.ps1.
# Then it opens Claude Code in this window on the last session (claude --continue, with Claude Code's normal
# permission prompts); -NoClaude skips that, -Fresh starts a new session instead of continuing.
#   powershell -ExecutionPolicy Bypass -File live\start.ps1
param([switch]$NoMic, [switch]$NoClaude, [switch]$Fresh)

$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "live\out\stack.pids"
New-Item -ItemType Directory -Force (Join-Path $root "live\out") | Out-Null   # logs and stack.pids; not in git
$old = @{}
if (Test-Path $pidFile) {
    foreach ($line in Get-Content $pidFile) {
        $name, $id = $line -split "=", 2
        if ($id) { $old[$name] = [int]$id }
    }
}
$pids = @{}

function Listening([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Alive([string]$name) {
    return $old.ContainsKey($name) -and [bool](Get-Process -Id $old[$name] -ErrorAction SilentlyContinue)
}

# Each service is created by Windows (WMI Win32_Process.Create), not as a child of this shell: started from a
# Claude Code session with Start-Process, the helper, brain and mic were cleaned up with that session's shell
# minutes later (2026-10-31: "mic is down again"). Created this way they belong to no shell, run as Dickie on his
# desktop (mic, mouse button, screen capture), and stay up until live\stop.ps1. Minimized windows, as before.
function Launch([string]$cmdline) {
    $si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]7 }
    # a local working folder: cmd started on the share warns "UNC paths are not supported" before pushd runs
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $cmdline; CurrentDirectory = $env:SystemRoot; ProcessStartupInformation = $si }
    if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed ($($r.ReturnValue)): $cmdline" }
    return [int]$r.ProcessId
}

# cmd's pushd maps the \\wsl.localhost share to a drive letter, and >> appends the bytes as they come
function StartCmd([string]$name, [string]$cmd) {
    $id = Launch "cmd.exe /c pushd `"$root`" && $cmd"
    $pids[$name] = $id
    Write-Host "started $name (pid $id)"
}

# $root is this repo on the WSL share (\\wsl.localhost\<distro>\home\...): the same folder seen from inside WSL
if ($root -match '^\\\\wsl(?:\.localhost|\$)\\([^\\]+)(\\.*)$') {
    $wslDistro = $Matches[1]; $wslRoot = $Matches[2] -replace '\\', '/'
} else { throw "live\start.ps1 must run from the repo on the WSL share (\\wsl.localhost\<distro>\...), not $root" }

function StartWsl([string]$name, [string]$dir, [string]$exe, [int]$port, [string]$log) {
    $sh = "cd $wslRoot/rust/$dir && exec ./target/release/$exe serve --port $port >> ../../live/out/$log 2>&1"
    $id = Launch "wsl.exe -d $wslDistro -- bash -lc `"$sh`""
    $pids[$name] = $id
    Write-Host "started $name on :$port (pid $id)"
}

if (Listening 8767) { Write-Host "coach-core already on :8767" } else {
    StartWsl "coach-core" "coach-core" "coach-core" 8767 "coach-core.log"
}
if (Listening 8766) { Write-Host "panel-reader already on :8766" } else {
    StartWsl "panel-reader" "panel-reader" "panel-reader" 8766 "rust-reader.log"
}
if (Listening 8765) { Write-Host "helper already on :8765" } else {
    StartCmd "helper" 'set "SC2_RULES=rust" && powershell -ExecutionPolicy Bypass -File live\run.ps1 >> live\out\live.log 2>&1'
}
if (Alive "brain") { Write-Host "brain already running (pid $($old['brain']))"; $pids["brain"] = $old["brain"] } else {
    StartCmd "brain" 'set "SC2_RULES=rust" && set "PYTHONIOENCODING=utf-8" && live\.python\python.exe -u live\brain_run.py >> live\out\brain-run.log 2>&1'
}
if ($NoMic) { Write-Host "mic skipped (-NoMic)" }
elseif (Alive "mic") { Write-Host "mic already running (pid $($old['mic']))"; $pids["mic"] = $old["mic"] } else {
    StartCmd "mic" 'powershell -ExecutionPolicy Bypass -File live\mic.ps1 >> live\out\mic-run.log 2>&1'
}

foreach ($k in $old.Keys) { if (-not $pids.ContainsKey($k) -and (Alive $k)) { $pids[$k] = $old[$k] } }
($pids.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) | Set-Content -Encoding ascii $pidFile

Start-Sleep -Seconds 8
foreach ($port in 8765, 8766, 8767) {
    $state = if (Listening $port) { "listening" } else { "NOT listening yet (see live/out logs)" }
    Write-Host ":$port $state"
}

if (-not $NoClaude) {
    Set-Location $root
    $flags = @()
    if (-not $Fresh) { $flags = @("--continue") }
    & claude @flags
}
