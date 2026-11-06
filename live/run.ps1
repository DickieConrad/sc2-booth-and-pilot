# Launch the timing helper with the Python and packages kept inside this folder (see setup.sh).
# Usage (Windows):  powershell -ExecutionPolicy Bypass -File live\run.ps1 [args for timing.py]
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $here ".pkgs"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
& (Join-Path $here ".python\python.exe") (Join-Path $here "timing.py") @args
exit $LASTEXITCODE
