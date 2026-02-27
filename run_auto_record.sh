#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <profile_task_name> [speed]"
  echo ""
  echo "Examples:"
  echo "  $0 p9_visual_organizer_T-05 1.0"
  echo "  $0 p1_methodical_T-05 0.5"
  echo ""
  echo "Available traces:"
  for d in "$(cd "$(dirname "$0")" && pwd)"/demo/p*/; do
    [ -f "$d/events_clean.json" ] && echo "  $(basename "$d")"
  done
  exit 1
}

PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  usage
fi
SPEED="${2:-1.0}"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_DIR="$REPO_ROOT/recordings"
SANDBOX_DIR="$REPO_ROOT/demo/pilot/sandbox/$PROFILE"
REPLAY_DIR="$REPO_ROOT/demo/$PROFILE"

# Validate inputs
if [ ! -d "$REPLAY_DIR" ]; then
  echo "ERROR: Replay directory not found: $REPLAY_DIR" >&2
  usage
fi
if [ ! -f "$REPLAY_DIR/events_clean.json" ]; then
  echo "ERROR: events_clean.json not found in: $REPLAY_DIR" >&2
  exit 1
fi
if [ ! -d "$SANDBOX_DIR" ]; then
  echo "WARNING: Sandbox directory not found: $SANDBOX_DIR"
  echo "         Creating empty sandbox..."
  mkdir -p "$SANDBOX_DIR"
fi

mkdir -p "$OUTPUT_DIR"

IMAGE="hippocamp-replay:latest"
CONTAINER="hippocamp-replay-${PROFILE}"

echo "=== Auto Record: $PROFILE (speed=$SPEED) ==="
echo ""

# Build image (only rebuilds if files changed)
echo "[1/3] Building Docker image..."
docker build \
  -f "$REPO_ROOT/docker/Dockerfile.replay" -t "$IMAGE" "$REPO_ROOT"

# Remove old container if exists
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Run container
echo "[2/3] Running replay + recording..."
docker run --rm \
  --shm-size=2g \
  --name "$CONTAINER" \
  -v "$SANDBOX_DIR:/hippocamp/data" \
  -v "$REPLAY_DIR:/hippocamp/replay:ro" \
  -v "$OUTPUT_DIR:/hippocamp/recordings" \
  -e DATASET_NAME="$PROFILE" \
  -e HIPPOCAMP_REPLAY_SPEED="$SPEED" \
  "$IMAGE"

echo ""
echo "[3/3] Done!"
if [ -f "$OUTPUT_DIR/$PROFILE.mp4" ]; then
  SIZE=$(du -h "$OUTPUT_DIR/$PROFILE.mp4" | cut -f1)
  echo "Video: $OUTPUT_DIR/$PROFILE.mp4 ($SIZE)"
else
  echo "ERROR: Video file not generated" >&2
  exit 1
fi
