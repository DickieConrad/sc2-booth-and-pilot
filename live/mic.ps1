# Launch push-to-talk callouts (mic.py) with the Python, packages and model weights kept inside this folder.
# Usage (Windows):  powershell -ExecutionPolicy Bypass -File live\mic.ps1 [--key f13] [--model small.en] [--device auto|cuda|cpu] [--no-note] [--no-fast]
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONPATH = Join-Path $here ".pkgs"
$env:HF_HOME = Join-Path $here ".models"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
& (Join-Path $here ".python\python.exe") (Join-Path $here "mic.py") @args
exit $LASTEXITCODE
