#!/usr/bin/env bash
# Start the coach's voice studio (VoiceStudio / OmniVoice) in its own Docker container, GPU-enabled,
# bound to loopback only. Models live in the named volume sc2coach-voice-data (survives upgrades).
# Stop it before playing:  docker stop -t 30 sc2coach-voice   (a plain stop gets killed mid-shutdown)
# Remove everything:      docker rm -f sc2coach-voice && docker volume rm sc2coach-voice-data
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
docker pull ghcr.io/debpalash/voicestudio:stable
docker rm -f sc2coach-voice >/dev/null 2>&1 || true
docker run -d --name sc2coach-voice --gpus all \
  -p 127.0.0.1:3900:3900 \
  -e OMNIVOICE_API_KEY="$OMNIVOICE_API_KEY" \
  -e OMNIVOICE_ANALYTICS_DISABLED=1 \
  -v sc2coach-voice-data:/app/omnivoice_data \
  ghcr.io/debpalash/voicestudio:stable
echo "started; first start downloads ~2.4 GB of model weights: docker logs -f sc2coach-voice"
