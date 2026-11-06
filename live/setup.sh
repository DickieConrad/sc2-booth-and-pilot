#!/usr/bin/env bash
# Run from WSL:  bash -lc "bash live/setup.sh"
# Puts a Windows CPython (live/.python/python.exe) and Windows wheels (live/.pkgs) inside this folder.
# Why from WSL: uv on Windows can't lock files on the \\wsl.localhost share, and `uv python install`
# for a Windows target fails on Linux, so we unpack the same python-build-standalone build uv picks.
set -euo pipefail
L="$(cd "$(dirname "$0")" && pwd)"
export UV_CACHE_DIR="$L/.uv-cache"
if [ ! -f "$L/.python/python.exe" ]; then
    url="$(uv python list cpython-3.12-windows-x86_64-none --all-platforms --show-urls | awk 'NR==1{print $2}')"
    echo "python: $url"
    rm -rf "$L/.python" && mkdir -p "$L/.python"
    curl -fsSL "$url" | tar -xz -C "$L/.python" --strip-components=1
fi
rm -rf "$L/.pkgs"
uv pip install --target "$L/.pkgs" --python-platform x86_64-pc-windows-msvc --python-version 3.12 \
    "mss>=10.0" "pillow>=10.0" "dxcam>=0.0.5" \
    "faster-whisper>=1.1" "sounddevice>=0.5" "pynput>=1.7" \
    "nvidia-cublas-cu12" "nvidia-cudnn-cu12>=9,<10" \
    "typesafe-sdk>=0.7" "pydantic>=2.14,<3"
# pydantic: the JSON boundary models (live/models.py) the helper, brain_run.py and watch.py validate with.
# mic.py (push-to-talk callouts): faster-whisper .. nvidia-*. The nvidia-* wheels (~2 GB) are the CUDA 12 cuBLAS /
# cuDNN 9 runtime DLLs that faster-whisper (CTranslate2) needs on the GPU; mic.py adds their bin folders to the DLL
# search path. typesafe-sdk: the optional Jev brain (live/brain.py, tools/eval_brain.py; key in live/.env.typesafe).
ls "$L/.python/python.exe"
