#!/usr/bin/env bash
# Run from WSL:  bash live/fetch_models.sh [small.en base.en ...]
# Fetches faster-whisper (CTranslate2) weights for mic.py into live/.models/faster-whisper-<name>/.
# Why not let mic.py download them: huggingface_hub's file lock fails on the \\wsl.localhost share from Windows.
set -euo pipefail
L="$(cd "$(dirname "$0")" && pwd)"
for m in "${@:-small.en}"; do
    d="$L/.models/faster-whisper-$m"
    [ -f "$d/model.bin" ] && { echo "have $m"; continue; }
    mkdir -p "$d"
    for f in config.json model.bin tokenizer.json vocabulary.txt; do
        curl -fsSL -o "$d/$f.part" "https://huggingface.co/Systran/faster-whisper-$m/resolve/main/$f"
        mv "$d/$f.part" "$d/$f"
    done
    echo "got $m: $(du -sh "$d" | cut -f1)"
done
