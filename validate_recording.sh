#!/usr/bin/env bash
# validate_recording.sh — Post-hoc validation of recorded videos
# Usage: ./validate_recording.sh <recording.mp4> [events.json]
set -euo pipefail

usage() {
  echo "Usage: $0 <recording.mp4> [profile_task_name]"
  echo ""
  echo "Validates a recording file using ffprobe and optionally compares"
  echo "against expected replay duration from events.json."
  echo ""
  echo "Examples:"
  echo "  $0 recordings/p10_silent_auditor_T-01.mp4"
  echo "  $0 recordings/p10_silent_auditor_T-01.mp4 p10_silent_auditor_T-01"
  exit 1
}

VIDEO="${1:-}"
if [ -z "$VIDEO" ] || [ ! -f "$VIDEO" ]; then
  echo "ERROR: Video file not found: $VIDEO" >&2
  usage
fi

PROFILE="${2:-}"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "=== Video Validation: $(basename "$VIDEO") ==="
echo ""

# Check ffprobe availability
if ! command -v ffprobe &>/dev/null; then
  echo "ERROR: ffprobe not found. Install ffmpeg." >&2
  exit 1
fi

# Extract video metadata
FILESIZE=$(du -h "$VIDEO" | cut -f1)
FILESIZE_BYTES=$(wc -c < "$VIDEO" | tr -d ' ')

DURATION=$(ffprobe -v quiet -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 "$VIDEO" 2>/dev/null || echo "0")

WIDTH=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=width \
  -of default=noprint_wrappers=1:nokey=1 "$VIDEO" 2>/dev/null || echo "0")

HEIGHT=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=height \
  -of default=noprint_wrappers=1:nokey=1 "$VIDEO" 2>/dev/null || echo "0")

FPS=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=r_frame_rate \
  -of default=noprint_wrappers=1:nokey=1 "$VIDEO" 2>/dev/null || echo "0/1")

CODEC=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=codec_name \
  -of default=noprint_wrappers=1:nokey=1 "$VIDEO" 2>/dev/null || echo "unknown")

NB_FRAMES=$(ffprobe -v quiet -select_streams v:0 -show_entries stream=nb_frames \
  -of default=noprint_wrappers=1:nokey=1 "$VIDEO" 2>/dev/null || echo "N/A")

echo "  File:       $(basename "$VIDEO")"
echo "  Size:       $FILESIZE ($FILESIZE_BYTES bytes)"
echo "  Duration:   ${DURATION}s"
echo "  Resolution: ${WIDTH}x${HEIGHT}"
echo "  FPS:        $FPS"
echo "  Codec:      $CODEC"
echo "  Frames:     $NB_FRAMES"

# Validation checks
ISSUES=0

# Check zero-size
if [ "$FILESIZE_BYTES" -lt 1000 ]; then
  echo "  [FAIL] File too small (< 1KB)"
  ISSUES=$((ISSUES + 1))
fi

# Check resolution
if [ "$WIDTH" != "1920" ] || [ "$HEIGHT" != "1080" ]; then
  echo "  [WARN] Resolution is ${WIDTH}x${HEIGHT}, expected 1920x1080"
fi

# Check duration > 0
DURATION_INT=$(python3 -c "print(int(float('${DURATION}')))" 2>/dev/null || echo "0")
if [ "$DURATION_INT" -lt 5 ]; then
  echo "  [FAIL] Video too short (< 5 seconds)"
  ISSUES=$((ISSUES + 1))
fi

# Compare with expected duration from events.json
if [ -n "$PROFILE" ]; then
  EVENTS_FILE="$REPO_ROOT/filegram_data/signal/$PROFILE/events.json"
  if [ -f "$EVENTS_FILE" ]; then
    EXPECTED=$(python3 -c "
import json
events = json.loads(open('$EVENTS_FILE').read())
behavioral = {'file_read','file_write','file_edit','file_move','file_rename','dir_create','file_copy','file_delete','file_search','file_browse','context_switch','cross_file_reference'}
bevts = [e for e in events if e.get('event_type') in behavioral]
if not bevts:
    print('0')
else:
    timestamps = [e.get('timestamp', 0) for e in bevts]
    if timestamps[0] > 1e12:
        span = (max(timestamps) - min(timestamps)) / 1000.0
    else:
        span = max(timestamps) - min(timestamps)
    print(f'{span:.1f}')
" 2>/dev/null || echo "0")

    EVENT_COUNT=$(python3 -c "
import json
events = json.loads(open('$EVENTS_FILE').read())
behavioral = {'file_read','file_write','file_edit','file_move','file_rename','dir_create','file_copy','file_delete','file_search','file_browse','context_switch','cross_file_reference'}
print(len([e for e in events if e.get('event_type') in behavioral]))
" 2>/dev/null || echo "?")

    echo ""
    echo "  Events:     $EVENT_COUNT behavioral events"
    echo "  Event span: ${EXPECTED}s (raw trajectory time)"
    echo "  Video dur:  ${DURATION}s"

    # Video should be longer than event span (due to animation overhead)
    # but not excessively so
    if [ "$EXPECTED" != "0" ]; then
      RATIO=$(python3 -c "print(f'{float(\"$DURATION\") / float(\"$EXPECTED\"):.2f}')" 2>/dev/null || echo "?")
      echo "  Ratio:      ${RATIO}x (video/events)"
    fi
  else
    echo "  [WARN] Events file not found: $EVENTS_FILE"
  fi
fi

echo ""
if [ "$ISSUES" -eq 0 ]; then
  echo "  RESULT: OK"
else
  echo "  RESULT: $ISSUES issue(s) found"
fi
