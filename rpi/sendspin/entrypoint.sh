#!/bin/sh
# Sendspin daemon entrypoint.
# Renders the Music Assistant player as "${HAL_DEVICE_NAME} Speaker"
# and posts music_playing state to the audio_streamer on stream
# start/stop so hardware volume buttons can target MA when music is
# playing. The audio_streamer listens on http://localhost:8080 (host
# networking on this container).

set -e

PLAYER_NAME="${HAL_DEVICE_NAME:-HAL} Speaker"
AUDIO_STREAMER_URL="${AUDIO_STREAMER_URL:-http://localhost:8080}"

HOOK_START="curl -fsS --max-time 2 -X POST ${AUDIO_STREAMER_URL}/api/music/state -H 'Content-Type: application/json' -d '{\"playing\":true}' || true"
HOOK_STOP="curl -fsS --max-time 2 -X POST ${AUDIO_STREAMER_URL}/api/music/state -H 'Content-Type: application/json' -d '{\"playing\":false}' || true"

exec sendspin daemon \
    --name "$PLAYER_NAME" \
    --hardware-volume false \
    --hook-start "$HOOK_START" \
    --hook-stop "$HOOK_STOP" \
    --log-level "${SENDSPIN_LOG_LEVEL:-INFO}"
