#!/usr/bin/env bash
# test_replay.sh — Run replay validation test for a single trace (no video recording)
# Usage: ./test_replay.sh <profile_task_name> [speed]
set -euo pipefail

usage() {
  echo "Usage: $0 <profile_task_name> [speed]"
  echo ""
  echo "Runs replay validation WITHOUT video recording."
  echo "Checks that all events are processed by the frontend."
  echo ""
  echo "Examples:"
  echo "  $0 p10_silent_auditor_T-01"
  echo "  $0 p10_silent_auditor_T-01 2.0"
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
TASK_NUM_PAD=$(python3 -c "print(f'{int(\"$TASK_NUM\"):02d}')")
WORKSPACE_DIR="$REPO_ROOT/filegram_data/workspace/t${TASK_NUM_PAD}_workspace"

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
CONTAINER="hippocamp-test-${PROFILE}"

echo "=== Replay Test: $PROFILE (speed=$SPEED) ==="
echo "Signal:    $SIGNAL_DIR"
echo "Workspace: $WORKSPACE_DIR"
echo ""

# Build image (reuses cache)
echo "[1/2] Building Docker image..."
docker build \
  -f "$REPO_ROOT/docker/Dockerfile.replay" -t "$IMAGE" "$REPO_ROOT"

# Remove old container if exists
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

# Run test container (uses test entrypoint instead of record_replay.sh)
echo "[2/2] Running replay test..."
docker run --rm \
  --shm-size=2g \
  --name "$CONTAINER" \
  -v "$WORKSPACE_DIR:/hippocamp/data" \
  -v "$SIGNAL_DIR:/hippocamp/replay:ro" \
  -v "$OUTPUT_DIR:/hippocamp/recordings" \
  -e DATASET_NAME="$PROFILE" \
  -e HIPPOCAMP_REPLAY_SPEED="$SPEED" \
  --entrypoint /hippocamp/test_replay.sh \
  "$IMAGE"

EXIT_CODE=$?

echo ""
if [ -f "$OUTPUT_DIR/${PROFILE}_report.json" ]; then
  echo "Report: $OUTPUT_DIR/${PROFILE}_report.json"
fi

exit $EXIT_CODE
