#!/usr/bin/env bash
# Build (and optionally flash) the PAL Round firmware inside Espressif's
# ESP-IDF container, so the host needs nothing but podman/docker.
#   ./build.sh                 build
#   ./build.sh flash [PORT]    build + flash + (no monitor)
#   ./build.sh menuconfig
set -euo pipefail
cd "$(dirname "$0")"
IMG=${IDF_IMAGE:-docker.io/espressif/idf:v5.5.2}
ENGINE=$(command -v podman || command -v docker)
args=(run --rm -v "$PWD":/project:Z -w /project -e HOME=/tmp -v pal-round-idf-cache:/tmp/.cache)
cmd=${1:-build}
case "$cmd" in
  flash)
    port=${2:-/dev/ttyACM0}
    args+=(--device "$port" --group-add keep-groups)
    exec "$ENGINE" "${args[@]}" "$IMG" idf.py -p "$port" -b 921600 flash ;;
  menuconfig)
    exec "$ENGINE" "${args[@]}" -it "$IMG" idf.py menuconfig ;;
  *)
    exec "$ENGINE" "${args[@]}" "$IMG" idf.py "$cmd" ;;
esac
