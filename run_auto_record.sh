#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <profile_task_name> [speed]"
  echo ""
  echo "Examples:"
  echo "  $0 p10_silent_auditor_T-01 1.0"
  echo "  $0 p1_methodical_T-05 0.5"
  echo ""
  echo "Data layout (filegram_data/):"
  echo "  signal/<profile_task>/events.json   - trajectory"
  echo "  workspace/tXX_workspace/            - initial file state"
  echo ""
  echo "Available traces:"
  for d in "$(cd "$(dirname "$0")" && pwd)"/filegram_data/signal/p*/; do
    [ -f "$d/events.json" ] && echo "  $(basename "$d")"
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
SIGNAL_DIR="$REPO_ROOT/filegram_data/signal/$PROFILE"

# Extract task number from profile name (e.g., p10_silent_auditor_T-01 -> 01)
TASK_NUM=$(echo "$PROFILE" | grep -oE 'T-[0-9]+$' | sed 's/T-//')
WORKSPACE_DIR="$REPO_ROOT/filegram_data/workspace/t$(printf '%02d' "$TASK_NUM")_workspace"

# Validate inputs
if [ ! -d "$SIGNAL_DIR" ]; then
  echo "ERROR: Signal directory not found: $SIGNAL_DIR" >&2
  usage
fi
if [ ! -f "$SIGNAL_DIR/events.json" ]; then
  echo "ERROR: events.json not found in: $SIGNAL_DIR" >&2
  exit 1
fi
if [ ! -d "$WORKSPACE_DIR" ]; then
  echo "WARNING: Workspace directory not found: $WORKSPACE_DIR"
  echo "         Creating empty workspace..."
  mkdir -p "$WORKSPACE_DIR"
fi

mkdir -p "$OUTPUT_DIR"

IMAGE="hippocamp-replay:latest"
CONTAINER="hippocamp-replay-${PROFILE}"

echo "=== Auto Record: $PROFILE (speed=$SPEED) ==="
echo "Signal:    $SIGNAL_DIR"
echo "Workspace: $WORKSPACE_DIR"
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
  -v "$WORKSPACE_DIR:/hippocamp/data" \
  -v "$SIGNAL_DIR:/hippocamp/replay:ro" \
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
