#!/usr/bin/env sh
# Migrate existing ./data/{source,jobs,tilesets,uploads} into the Docker named volume.
# Usage (from repo root): sh scripts/migrate-data-to-volume.sh

set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VOLUME_NAME="${WORKSPACE_DOCKER_VOLUME:-ocean-terrain-handler_workspace_data}"
HOST_DATA="$ROOT/data"

if [ ! -d "$HOST_DATA" ]; then
  echo "Host data directory not found: $HOST_DATA" >&2
  exit 1
fi

echo "Ensuring volume '$VOLUME_NAME' exists..."
docker compose create api >/dev/null

echo "Copying source/jobs/tilesets/uploads from $HOST_DATA into volume $VOLUME_NAME ..."
docker run --rm \
  -v "${VOLUME_NAME}:/to" \
  -v "${HOST_DATA}:/from:ro" \
  alpine:3.20 \
  sh -c 'set -e
    mkdir -p /to
    for d in source jobs tilesets uploads; do
      if [ -d "/from/$d" ]; then
        echo "-> $d"
        rm -rf "/to/$d"
        cp -a "/from/$d" /to/
      fi
    done
    mkdir -p /to/source /to/jobs /to/uploads /to/tilesets/terrain
    echo "Volume contents:"
    ls -la /to
    echo "source entries:"
    ls /to/source | wc -l'

echo
echo "Done. Named volume: $VOLUME_NAME"
echo "Next: docker compose up -d --build"
echo "Workspace (including source/) lives in the named volume."
echo "To add more DEM later: sh scripts/copy-to-workspace-volume.sh <host-path> [dest-rel]"
