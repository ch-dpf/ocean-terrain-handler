#!/usr/bin/env sh
# Copy a host file or directory into the workspace Docker volume.
# Usage:
#   sh scripts/copy-to-workspace-volume.sh /path/to/dem.tif
#   sh scripts/copy-to-workspace-volume.sh /path/to/folder source/gDEM_N

set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VOLUME_NAME="${WORKSPACE_DOCKER_VOLUME:-ocean-terrain-handler_workspace_data}"

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <host-path> [dest-relative-under-workspace]" >&2
  exit 1
fi

HOST_PATH="$1"
if [ ! -e "$HOST_PATH" ]; then
  echo "Path not found: $HOST_PATH" >&2
  exit 1
fi

LEAF="$(basename "$HOST_PATH")"
DEST_REL="${2:-source/$LEAF}"
DEST_REL="$(echo "$DEST_REL" | sed 's|^/||')"

echo "Copying '$HOST_PATH' -> volume:$VOLUME_NAME /$DEST_REL"

if [ -d "$HOST_PATH" ]; then
  docker run --rm \
    -v "${VOLUME_NAME}:/data/workspace" \
    -v "${HOST_PATH}:/from:ro" \
    alpine:3.20 \
    sh -c "mkdir -p '/data/workspace/$DEST_REL' && cp -a /from/. '/data/workspace/$DEST_REL/'"
else
  DEST_DIR="$(dirname "$DEST_REL")"
  FILE_NAME="$(basename "$DEST_REL")"
  docker run --rm \
    -v "${VOLUME_NAME}:/data/workspace" \
    -v "${HOST_PATH}:/from/file:ro" \
    alpine:3.20 \
    sh -c "mkdir -p '/data/workspace/$DEST_DIR' && cp /from/file '/data/workspace/$DEST_DIR/$FILE_NAME'"
fi

echo "Done."
