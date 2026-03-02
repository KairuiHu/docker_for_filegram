#!/usr/bin/env bash
# batch_generate.sh — Smart batch recording with pre-filtering and resume support
# Usage: ./batch_generate.sh [--min-category MODERATE] [--test-only] [--limit N] [trace1 trace2 ...]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SIGNAL_DIR="$REPO_ROOT/filegram_data/signal"
RECORDINGS_DIR="$REPO_ROOT/recordings"
REPORT_FILE="$RECORDINGS_DIR/batch_report.json"

MIN_CATEGORY="SIMPLE"  # RICH > MODERATE > SIMPLE > MINIMAL
TEST_ONLY=false
LIMIT=0
TRACES=()

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --min-category)
      MIN_CATEGORY="${2:-SIMPLE}"
      shift 2
      ;;
    --test-only)
      TEST_ONLY=true
      shift
      ;;
    --limit)
      LIMIT="${2:-0}"
      shift 2
      ;;
    --help|-h)
      echo "Usage: $0 [options] [trace1 trace2 ...]"
      echo ""
      echo "Options:"
      echo "  --min-category CAT  Minimum category: RICH, MODERATE, SIMPLE, MINIMAL (default: SIMPLE)"
      echo "  --test-only         Run validation only, no video recording"
      echo "  --limit N           Process at most N traces"
      echo ""
      echo "If no traces specified, auto-selects from filegram_data/signal/ based on category filter."
      echo "Already-recorded traces are skipped (resume support)."
      exit 0
      ;;
    *)
      TRACES+=("$1")
      shift
      ;;
  esac
done

mkdir -p "$RECORDINGS_DIR"

# Category ranking for filtering
category_rank() {
  case "$1" in
    RICH) echo 4 ;;
    MODERATE) echo 3 ;;
    SIMPLE) echo 2 ;;
    MINIMAL) echo 1 ;;
    *) echo 0 ;;
  esac
}

MIN_RANK=$(category_rank "$MIN_CATEGORY")

# If no traces specified, auto-select using analyze_best_demos.py logic
if [ ${#TRACES[@]} -eq 0 ]; then
  echo "Auto-selecting traces (min category: $MIN_CATEGORY)..."

  # Use python to analyze and filter traces
  TRACES_STR=$(python3 -c "
import json, os, sys
from pathlib import Path
from collections import Counter

signal_dir = Path('$SIGNAL_DIR')
recordings_dir = Path('$RECORDINGS_DIR')
min_rank = $MIN_RANK

BEHAVIORAL = {
    'file_read', 'file_write', 'file_edit', 'file_move', 'file_rename',
    'dir_create', 'file_copy', 'file_delete', 'file_search', 'file_browse',
    'context_switch', 'cross_file_reference', 'error_encounter', 'error_response',
}

ranks = {'RICH': 4, 'MODERATE': 3, 'SIMPLE': 2, 'MINIMAL': 1}

results = []
for d in sorted(signal_dir.iterdir()):
    if not d.is_dir() or d.name.startswith(('__', '.')):
        continue
    ef = d / 'events.json'
    if not ef.exists():
        continue

    # Skip already recorded
    mp4 = recordings_dir / f'{d.name}.mp4'
    if mp4.exists():
        continue

    # Check workspace exists
    parts = d.name.rsplit('_T-', 1)
    if len(parts) != 2:
        continue
    try:
        task_num = int(parts[1])
    except ValueError:
        continue
    ws = signal_dir.parent / 'workspace' / f't{task_num:02d}_workspace'
    if not ws.exists():
        continue

    try:
        events = json.loads(ef.read_text())
    except Exception:
        continue
    if not events:
        continue

    # Check session_end
    has_end = any(e.get('event_type') == 'session_end' for e in events)
    if not has_end:
        continue

    behavioral = [e for e in events if e.get('event_type') in BEHAVIORAL]
    n_types = len(set(e.get('event_type') for e in behavioral))

    if n_types >= 6: cat = 'RICH'
    elif n_types >= 4: cat = 'MODERATE'
    elif n_types >= 2: cat = 'SIMPLE'
    else: cat = 'MINIMAL'

    if ranks.get(cat, 0) < min_rank:
        continue

    results.append((d.name, cat, n_types, len(behavioral)))

# Sort: RICH first, then by event count
results.sort(key=lambda r: (ranks.get(r[1], 0), r[3]), reverse=True)

for name, cat, nt, ne in results:
    print(name)
" 2>/dev/null)

  if [ -z "$TRACES_STR" ]; then
    echo "No traces found matching criteria."
    exit 0
  fi

  # Convert to array
  while IFS= read -r line; do
    TRACES+=("$line")
  done <<< "$TRACES_STR"
fi

# Apply limit
if [ "$LIMIT" -gt 0 ] && [ ${#TRACES[@]} -gt "$LIMIT" ]; then
  TRACES=("${TRACES[@]:0:$LIMIT}")
fi

TOTAL=${#TRACES[@]}
echo ""
echo "=== Batch ${TEST_ONLY:+Test}${TEST_ONLY:+}${TEST_ONLY:-Generate}: $TOTAL traces ==="
echo ""

# Print trace list
for i in "${!TRACES[@]}"; do
  echo "  $((i+1)). ${TRACES[$i]}"
done
echo ""

# Initialize batch report
PASSED=0
FAILED=0
SKIPPED=0
RESULTS=()

for i in "${!TRACES[@]}"; do
  TRACE="${TRACES[$i]}"
  IDX=$((i+1))
  echo "[$IDX/$TOTAL] $TRACE"

  # Check if already recorded (double-check for race conditions)
  if [ "$TEST_ONLY" = false ] && [ -f "$RECORDINGS_DIR/${TRACE}.mp4" ]; then
    echo "  SKIP (already recorded)"
    SKIPPED=$((SKIPPED + 1))
    RESULTS+=("{\"trace\":\"$TRACE\",\"status\":\"skipped\"}")
    continue
  fi

  # Run test or record
  START_TIME=$(date +%s)
  if [ "$TEST_ONLY" = true ]; then
    if "$REPO_ROOT/test_replay.sh" "$TRACE" 2.0; then
      STATUS="pass"
      PASSED=$((PASSED + 1))
    else
      STATUS="fail"
      FAILED=$((FAILED + 1))
    fi
  else
    if "$REPO_ROOT/run_auto_record.sh" "$TRACE" 1.0; then
      STATUS="pass"
      PASSED=$((PASSED + 1))
    else
      STATUS="fail"
      FAILED=$((FAILED + 1))
    fi
  fi
  END_TIME=$(date +%s)
  DURATION=$((END_TIME - START_TIME))

  echo "  $STATUS (${DURATION}s)"
  RESULTS+=("{\"trace\":\"$TRACE\",\"status\":\"$STATUS\",\"duration_s\":$DURATION}")
  echo ""
done

# Write batch report
echo ""
echo "=== Batch Summary ==="
echo "  Total:   $TOTAL"
echo "  Passed:  $PASSED"
echo "  Failed:  $FAILED"
echo "  Skipped: $SKIPPED"

# Generate JSON report
python3 -c "
import json, sys
results = [$(IFS=,; echo "${RESULTS[*]:-}")]
report = {
    'total': $TOTAL,
    'passed': $PASSED,
    'failed': $FAILED,
    'skipped': $SKIPPED,
    'results': results,
}
with open('$REPORT_FILE', 'w') as f:
    json.dump(report, f, indent=2)
print(f'Report saved: $REPORT_FILE')
" 2>/dev/null || true

exit $( [ "$FAILED" -eq 0 ] && echo 0 || echo 1 )
