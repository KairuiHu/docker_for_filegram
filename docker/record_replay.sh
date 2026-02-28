#!/usr/bin/env bash
set -euo pipefail

RECORDING_PATH="/hippocamp/recordings/${DATASET_NAME:-replay}.mp4"
EVENTS_FILE="${HIPPOCAMP_REPLAY_EVENTS:-/hippocamp/replay/events.json}"
SPEED="${HIPPOCAMP_REPLAY_SPEED:-1.0}"
WEBUI_PORT="${HIPPOCAMP_PORT:-8080}"
WEBUI_URL="http://localhost:$WEBUI_PORT"

echo "=== HippoCamp Auto Record ==="
echo "Events:    $EVENTS_FILE"
echo "Speed:     $SPEED"
echo "Output:    $RECORDING_PATH"
echo ""

# 1. Start Xvfb virtual display
echo "[1/7] Starting Xvfb virtual display..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!
sleep 1

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  echo "ERROR: Xvfb failed to start" >&2
  exit 1
fi

# 2. Start WebUI server
echo "[2/7] Starting WebUI server..."
python3 /hippocamp/webui/app.py &
WEBUI_PID=$!

# Wait for WebUI to be ready
for i in $(seq 1 30); do
  if curl -fsS "$WEBUI_URL/api/replay/status" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$WEBUI_PID" 2>/dev/null; then
    echo "ERROR: WebUI process died" >&2
    exit 1
  fi
  sleep 0.5
done

if ! curl -fsS "$WEBUI_URL/api/replay/status" >/dev/null 2>&1; then
  echo "ERROR: WebUI did not start in time" >&2
  exit 1
fi
echo "       WebUI ready at $WEBUI_URL"

# 3. Open Chromium browser in kiosk mode
echo "[3/7] Opening Chromium browser..."
chromium --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --window-size=1920,1080 \
  --disable-software-rasterizer \
  --disable-extensions \
  "$WEBUI_URL" &
BROWSER_PID=$!

# Wait for Chromium to initialize (longer wait needed under x86 emulation on ARM)
echo "       Waiting for browser to initialize..."
for i in $(seq 1 20); do
  if ! kill -0 "$BROWSER_PID" 2>/dev/null; then
    echo "ERROR: Chromium process died during startup" >&2
    exit 1
  fi
  sleep 1
done
echo "       Browser ready"

# 4. Start ffmpeg screen recording
echo "[4/7] Starting screen recording..."
ffmpeg -y -f x11grab -video_size 1920x1080 -framerate 30 -i :99 \
  -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p \
  "$RECORDING_PATH" </dev/null >/dev/null 2>&1 &
FFMPEG_PID=$!
sleep 1

if ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
  echo "ERROR: ffmpeg failed to start" >&2
  exit 1
fi

# 5. Trigger replay
echo "[5/7] Triggering replay..."
REPLAY_RESULT=$(curl -s -X POST "$WEBUI_URL/api/replay/start" \
  -H 'Content-Type: application/json' \
  -d "{\"path\":\"$EVENTS_FILE\",\"speed\":$SPEED}")
echo "       $REPLAY_RESULT"

# 6. Wait for replay to complete
echo "[6/7] Waiting for replay to complete..."
MAX_WAIT=600  # 10 minutes max
ELAPSED=0
while true; do
  COMPLETED=$(curl -s "$WEBUI_URL/api/replay/status" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',{}).get('completed',False))" 2>/dev/null || echo "False")
  if [ "$COMPLETED" = "True" ]; then
    echo "       Replay completed!"
    sleep 2  # let final frames render
    break
  fi
  ELAPSED=$((ELAPSED + 1))
  if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo "ERROR: Replay timed out after ${MAX_WAIT}s" >&2
    break
  fi
  sleep 5
done

# 7. Stop recording and clean up
echo "[7/7] Stopping recording..."
kill -INT "$FFMPEG_PID" 2>/dev/null || true
wait "$FFMPEG_PID" 2>/dev/null || true

kill "$BROWSER_PID" 2>/dev/null || true
kill "$WEBUI_PID" 2>/dev/null || true
kill "$XVFB_PID" 2>/dev/null || true

echo ""
echo "=== Done ==="
echo "Recording saved: $RECORDING_PATH"

if [ -f "$RECORDING_PATH" ]; then
  SIZE=$(du -h "$RECORDING_PATH" | cut -f1)
  echo "File size: $SIZE"
else
  echo "WARNING: Recording file not found!"
  exit 1
fi
