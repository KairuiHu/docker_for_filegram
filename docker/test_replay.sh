#!/usr/bin/env bash
# test_replay.sh — Container entrypoint for replay validation (no video recording)
# Runs Xvfb + WebUI + Chromium, triggers replay, waits for completion,
# then fetches /api/replay/report and prints pass/fail summary.
set -euo pipefail

EVENTS_FILE="${HIPPOCAMP_REPLAY_EVENTS:-/hippocamp/replay/events.json}"
SPEED="${HIPPOCAMP_REPLAY_SPEED:-1.0}"
WEBUI_PORT="${HIPPOCAMP_PORT:-8080}"
WEBUI_URL="http://localhost:$WEBUI_PORT"
REPORT_PATH="/hippocamp/recordings/${DATASET_NAME:-replay}_report.json"

echo "=== HippoCamp Replay Test ==="
echo "Events:    $EVENTS_FILE"
echo "Speed:     $SPEED"
echo ""

# 1. Start Xvfb virtual display
echo "[1/5] Starting Xvfb..."
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
XVFB_PID=$!
sleep 1

if ! kill -0 "$XVFB_PID" 2>/dev/null; then
  echo "ERROR: Xvfb failed to start" >&2
  exit 1
fi

# 2. Start WebUI server
echo "[2/5] Starting WebUI..."
python3 /hippocamp/webui/app.py &
WEBUI_PID=$!

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
echo "       WebUI ready"

# 3. Open Chromium
echo "[3/5] Opening Chromium..."
chromium --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --window-size=1920,1080 \
  --disable-software-rasterizer \
  --disable-extensions \
  "$WEBUI_URL" &
BROWSER_PID=$!

echo "       Waiting for browser..."
for i in $(seq 1 20); do
  if ! kill -0 "$BROWSER_PID" 2>/dev/null; then
    echo "ERROR: Chromium died during startup" >&2
    exit 1
  fi
  sleep 1
done
echo "       Browser ready"

# 4. Trigger replay
echo "[4/5] Triggering replay..."
REPLAY_RESULT=$(curl -s -X POST "$WEBUI_URL/api/replay/start" \
  -H 'Content-Type: application/json' \
  -d "{\"path\":\"$EVENTS_FILE\",\"speed\":$SPEED}")
echo "       $REPLAY_RESULT"

# Wait for replay to complete (all events sent)
MAX_WAIT=600
ELAPSED=0
while true; do
  STATUS_JSON=$(curl -s "$WEBUI_URL/api/replay/status" 2>/dev/null || echo "{}")
  COMPLETED=$(echo "$STATUS_JSON" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('state',{}).get('completed',False))" 2>/dev/null || echo "False")
  if [ "$COMPLETED" = "True" ]; then
    echo "       All events sent!"
    break
  fi
  ELAPSED=$((ELAPSED + 5))
  if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo "ERROR: Replay timed out after ${MAX_WAIT}s" >&2
    break
  fi
  sleep 5
done

# Wait for frontend to process events
# Frontend animations are slow (~30-60s per event), so we wait for
# a reasonable sample or full completion with a generous timeout.
echo "       Waiting for frontend to process events..."
ACK_WAIT=0
ACK_MAX=600  # 10 minutes max
LAST_ACKED=0
STALL_COUNT=0
while true; do
  ACK_STATUS=$(curl -s "$WEBUI_URL/api/replay/report" 2>/dev/null || echo "{}")
  ACKED=$(echo "$ACK_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('report',{}).get('events_acked',0))" 2>/dev/null || echo "0")
  TOTAL_EVTS=$(echo "$ACK_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('report',{}).get('events_total',0))" 2>/dev/null || echo "0")
  FAILED=$(echo "$ACK_STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('report',{}).get('events_failed',0))" 2>/dev/null || echo "0")

  if [ "$ACKED" = "$TOTAL_EVTS" ] && [ "$TOTAL_EVTS" != "0" ]; then
    echo "       All $ACKED/$TOTAL_EVTS events acknowledged!"
    break
  fi

  # Check for progress stall
  if [ "$ACKED" = "$LAST_ACKED" ]; then
    STALL_COUNT=$((STALL_COUNT + 1))
  else
    STALL_COUNT=0
    LAST_ACKED="$ACKED"
  fi

  # If stalled for 60s (12 checks * 5s) and we have some acks, consider done
  if [ "$STALL_COUNT" -ge 12 ] && [ "$ACKED" -gt 0 ]; then
    echo "       Frontend stalled at $ACKED/$TOTAL_EVTS events (no progress for 60s)"
    break
  fi

  echo "       Acked: $ACKED / $TOTAL_EVTS (failed: $FAILED)"
  ACK_WAIT=$((ACK_WAIT + 5))
  if [ $ACK_WAIT -ge $ACK_MAX ]; then
    echo "       Timeout at $ACKED/$TOTAL_EVTS events"
    break
  fi
  sleep 5
done

# 5. Fetch and display report
echo "[5/5] Fetching test report..."
REPORT=$(curl -s "$WEBUI_URL/api/replay/report")

# Save report and display results
echo "$REPORT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
report = data.get('report', {})
with open('$REPORT_PATH', 'w') as f:
    json.dump(report, f, indent=2)

total = report.get('events_total', 0)
sent = report.get('events_sent', 0)
acked = report.get('events_acked', 0)
failed = report.get('events_failed', 0)
skipped = report.get('events_skipped', 0)
unhandled = report.get('events_unhandled', 0)
coverage = round(100 * acked / total, 1) if total else 0

print()
print('=' * 60)
print('REPLAY TEST REPORT')
print('=' * 60)
print(f'  Events total:     {total}')
print(f'  Events sent:      {sent}')
print(f'  Events acked:     {acked} ({coverage}%)')
print(f'  Events failed:    {failed}')
print(f'  Events skipped:   {skipped}')
print(f'  Events unhandled: {unhandled}')
print()
ds = report.get('duration_stats', {})
print(f'  Duration (avg):   {ds.get(\"avg_ms\", 0):.0f}ms per event')
print(f'  Duration (total): {ds.get(\"total_ms\", 0)/1000:.1f}s')

# Pass criteria:
# 1. All events were sent (backend ok)
# 2. No events failed (no handler errors)
# 3. At least some events were acked (frontend connected and processing)
passed = (sent == total and failed == 0 and acked > 0)

print()
if passed:
    print('  RESULT: PASS')
else:
    reasons = []
    if sent != total: reasons.append(f'sent {sent}/{total}')
    if failed > 0: reasons.append(f'{failed} failed')
    if acked == 0: reasons.append('no acks received')
    print(f'  RESULT: FAIL ({\"  \".join(reasons)})')
    failed_list = report.get('failed_events', [])
    if failed_list:
        print(f'  Failed events ({len(failed_list)}):')
        for fe in failed_list:
            print(f'    [{fe.get(\"index\")}] {fe.get(\"event_type\")}: {fe.get(\"error\", \"unknown\")}')
print('=' * 60)
sys.exit(0 if passed else 1)
"
EXIT_CODE=$?

# Cleanup
kill "$BROWSER_PID" 2>/dev/null || true
kill "$WEBUI_PID" 2>/dev/null || true
kill "$XVFB_PID" 2>/dev/null || true

echo ""
echo "Report saved: $REPORT_PATH"
exit $EXIT_CODE
