# HippoCamp Replay Recorder

Record AI agent file-operation traces as MP4 videos. The system replays timestamped events (file reads, writes, moves, directory creation, etc.) through a browser-based GUI inside Docker, and captures the screen with ffmpeg.

## Prerequisites

- Docker Desktop (with ARM64 support on Apple Silicon)
- bash, ffmpeg (for verification)

## Directory Structure

```
.
├── run_auto_record.sh          # Entry point: build + record one trace
├── docker/
│   ├── Dockerfile.replay       # Container image (Xvfb + Chromium + Flask)
│   ├── record_replay.sh        # In-container 7-step pipeline
│   └── webui/                  # Flask + SocketIO web UI
│       ├── app.py              # Backend: replay engine, file browser, APIs
│       ├── templates/index.html# Frontend: animated replay visualization
│       ├── start_webui.sh
│       ├── sync_wrappers
│       ├── terminal_sync.py
│       ├── webui_status.sh
│       └── webui_stop.sh
├── demo/                       # YOUR DATA (not in repo)
│   ├── <profile>_<task>/       # Trajectory: events_clean.json + media/
│   └── pilot/sandbox/<profile>_<task>/  # Workspace: initial file state
└── recordings/                 # Output MP4 files (not in repo)
```

## Input Format

Each recording requires two inputs:

### 1. Trajectory (`demo/<name>/events_clean.json`)

A JSON array of timestamped behavioral events:

```json
[
  {"event_type": "file_read",   "timestamp": 22.5, "file_path": "report.md", ...},
  {"event_type": "dir_create",  "timestamp": 64.6, "dir_path": "01_docs", ...},
  {"event_type": "file_move",   "timestamp": 80.4, "old_path": "report.md", "new_path": "01_docs", ...},
  {"event_type": "file_write",  "timestamp": 119.7, "file_path": "01_docs/README.md", ...}
]
```

Supported event types: `file_read`, `file_write`, `file_edit`, `file_move`, `file_rename`, `file_copy`, `file_delete`, `file_search`, `file_browse`, `dir_create`, `context_switch`, `cross_file_reference`, `error_encounter`, `error_response`.

### 2. Workspace (`demo/pilot/sandbox/<name>/`)

The **initial** filesystem state before the agent acts. Place all files the agent will interact with here. This directory is mounted into the container at `/hippocamp/data` and displayed as the file tree in the GUI.

**Important:** The workspace must be the *starting* state, not the end state. The replay engine only visualizes events -- it does not execute actual file operations on disk.

## Usage

### Record a single trace

```bash
./run_auto_record.sh <profile_task_name> [speed]

# Examples:
./run_auto_record.sh p9_visual_organizer_T-05 1.0
./run_auto_record.sh p1_methodical_T-05 0.5   # half speed
```

Output: `recordings/<profile_task_name>.mp4`

### Batch record all traces

```bash
for d in demo/p*/; do
  name=$(basename "$d")
  [ "$name" = "pilot" ] && continue
  [ ! -f "$d/events_clean.json" ] && continue
  [ -f "recordings/${name}.mp4" ] && echo "SKIP: $name" && continue
  ./run_auto_record.sh "$name" 1.0
done
```

### Use your own data

1. Create your trajectory file:
   ```
   demo/my_task/events_clean.json
   ```

2. Create your workspace directory with the initial files:
   ```
   demo/pilot/sandbox/my_task/
   ├── file1.txt
   ├── file2.pdf
   └── ...
   ```

3. Run:
   ```bash
   ./run_auto_record.sh my_task 1.0
   ```

## How It Works

Inside the Docker container, `record_replay.sh` runs a 7-step pipeline:

1. Start Xvfb virtual display (1920x1080)
2. Start Flask + SocketIO web server
3. Open Chromium browser to localhost:8080
4. Start ffmpeg screen recording
5. POST `/api/replay/start` with the events file path
6. Poll `/api/replay/status` until completion
7. Stop recording, output MP4

The web UI receives events via WebSocket and animates them: cursor movement, file opening/reading with scroll, directory creation, file drag-and-drop moves, context menus, toast notifications, etc.

## Notes

- Runs natively on ARM64 (Apple Silicon). No x86 emulation needed.
- `--shm-size=2g` is required for Chromium stability.
- dbus errors in the logs are harmless (no system bus in container).
- Video resolution: 1920x1080 @ 30fps.
