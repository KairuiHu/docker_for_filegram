#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HippoCamp WebUI - Flask backend with WebSocket for real-time Terminal sync

Features:
- File browser with tree view
- 3 APIs: return_txt, return_img, return_ori
- Bidirectional sync: Terminal <-> WebUI
- Real-time command visualization
"""
import os
import sys
import json
import threading
import subprocess
import time
import shlex
import re
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, send_file
from flask_socketio import SocketIO, emit

def load_runtime_config():
    path = os.environ.get('HIPPOCAMP_RUNTIME_CONFIG', '/hippocamp/runtime_config.py')
    if not os.path.exists(path):
        return {}
    try:
        data = {}
        with open(path, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(compile(code, path, 'exec'), {}, data)
        return data
    except Exception:
        return {}


RUNTIME_CONFIG = load_runtime_config()

# Port configuration - env overrides runtime config, which overrides default
WEBUI_PORT = int(os.environ.get('HIPPOCAMP_PORT') or RUNTIME_CONFIG.get('PORT', 8080))

# Add api directory to path
sys.path.insert(0, '/hippocamp/api')
try:
    from hippocamp_api import return_txt as api_return_txt
    from hippocamp_api import return_img as api_return_img
    from hippocamp_api import return_ori as api_return_ori
    from hippocamp_api import get_metadata as api_get_metadata
    from hippocamp_api import list_files as api_list_files
except ImportError:
    # For local development/testing
    def api_return_txt(path):
        return {"success": False, "error": "API not available in dev mode"}
    def api_return_img(path, out=None):
        return {"success": False, "error": "API not available in dev mode"}
    def api_return_ori(path, out=None):
        return {"success": False, "error": "API not available in dev mode"}
    def api_get_metadata(path):
        return {"success": False, "error": "API not available in dev mode"}
    def api_list_files():
        return []

app = Flask(__name__)
app.config['SECRET_KEY'] = 'hippocamp-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Directories
DATA_DIR = Path(os.environ.get('HIPPOCAMP_DATA_DIR', '/hippocamp/data'))
GOLD_DIR = Path(os.environ.get('HIPPOCAMP_GOLD_DIR', '/hippocamp/gold'))
OUTPUT_DIR = Path(os.environ.get('HIPPOCAMP_OUTPUT_DIR', '/hippocamp/output'))
METADATA_DIR = Path(os.environ.get('HIPPOCAMP_METADATA_DIR', '/hippocamp/metadata'))
TMP_UI_DIR = Path(os.environ.get('HIPPOCAMP_TMP_UI_DIR', '/tmp/hippocamp_webui'))
TMP_UI_OPS_FILE = TMP_UI_DIR / 'ui_ops.jsonl'

# Dataset info
DATASET_NAME = os.environ.get('DATASET_NAME', 'HippoCamp')
DATASET_USER = os.environ.get('DATASET_USER', 'unknown')

# Feature flags (set via terminal command set_flags; optional)
FEATURE_FLAGS_PATH = Path(os.environ.get('HIPPOCAMP_FEATURE_FLAGS', '/hippocamp/metadata/feature_flags.json'))
REPLAY_DEFAULT_EVENTS_PATH = Path(os.environ.get('HIPPOCAMP_REPLAY_EVENTS', '/hippocamp/replay/events_clean.json'))
REPLAY_AUTOSTART = os.environ.get('HIPPOCAMP_REPLAY_AUTOSTART', '0') == '1'

try:
    REPLAY_DEFAULT_SPEED = max(0.05, float(os.environ.get('HIPPOCAMP_REPLAY_SPEED', '1.0')))
except Exception:
    REPLAY_DEFAULT_SPEED = 1.0

replay_state_lock = threading.Lock()
replay_state = {
    'running': False,
    'completed': False,
    'session_id': 0,
    'events_path': '',
    'speed': REPLAY_DEFAULT_SPEED,
    'events_total': 0,
    'events_sent': 0,
    'last_event_type': '',
    'last_event_ts': None,
    'started_at': None,
    'finished_at': None,
    'error': None,
}
replay_clients_lock = threading.Lock()
replay_clients_cv = threading.Condition(replay_clients_lock)
replay_connected_clients = 0
REPLAY_NO_CLIENT_GATE = os.environ.get('HIPPOCAMP_REPLAY_NO_CLIENT_GATE', '0') == '1'


def load_feature_flags():
    if FEATURE_FLAGS_PATH.exists():
        try:
            with open(FEATURE_FLAGS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
            return {
                'initialized': True,
                'enable_return_txt': bool(data.get('enable_return_txt', True)),
                'enable_return_img': bool(data.get('enable_return_img', True)),
            }
        except Exception:
            return {'initialized': True, 'enable_return_txt': True, 'enable_return_img': True}
    return {'initialized': False, 'enable_return_txt': True, 'enable_return_img': True}


def init_feature_flags(enable_txt, enable_img):
    """Set feature flags (can be updated anytime)."""
    try:
        FEATURE_FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'enable_return_txt': bool(enable_txt),
            'enable_return_img': bool(enable_img),
            'updated_at': datetime.utcnow().isoformat() + 'Z'
        }
        with open(FEATURE_FLAGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        return {'success': True, **payload}
    except Exception as e:
        return {'success': False, 'error': str(e)}

# Command history for sync
command_history = []
MAX_HISTORY = 100
RETURN_TXT_PREVIEW_MAX = 50000
RETURN_ORI_PREVIEW_MAX = 50000

# Current working directory for WebUI shell (persistent across commands)
current_directory = '/hippocamp/data'

# Terminal watcher thread
terminal_watcher_running = False


def reset_tmp_ui_state():
    """Reset temporary UI state storage (ephemeral per WebUI process run)."""
    try:
        TMP_UI_DIR.mkdir(parents=True, exist_ok=True)
        with open(TMP_UI_OPS_FILE, 'w', encoding='utf-8') as f:
            f.write('')
    except Exception as e:
        print(f"Warning: could not reset tmp ui state: {e}", flush=True)


def append_tmp_ui_event(action, payload=None):
    """Append a UI event to tmp storage for observability during current run."""
    try:
        TMP_UI_DIR.mkdir(parents=True, exist_ok=True)
        event = {
            'action': str(action or '').strip() or 'unknown',
            'payload': payload or {},
            'ts': datetime.now(timezone.utc).isoformat()
        }
        with open(TMP_UI_OPS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
        return {'success': True, 'event': event}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _timestamp_payload():
    now = datetime.now(timezone.utc)
    return {
        'timestamp': now.isoformat(),
        'ts_ms': int(now.timestamp() * 1000)
    }


def log_command(source, command, result=None, is_error=False):
    """Log a command to history and broadcast to WebUI"""
    entry = {
        'source': source,  # 'terminal' or 'webui'
        'command': command,
        'result': result,
        'is_error': is_error
    }
    entry.update(_timestamp_payload())
    command_history.append(entry)
    if len(command_history) > MAX_HISTORY:
        command_history.pop(0)

    # Broadcast to all connected WebUI clients
    socketio.emit('command_executed', entry)

    # Print to Docker terminal for visibility
    if source == 'webui':
        print(f"\n\033[94m[WebUI]\033[0m {entry['timestamp']} \033[93m$ {command}\033[0m", flush=True)
        if result:
            if result.get('success'):
                print(f"\033[94m[WebUI]\033[0m \033[92mSuccess\033[0m", flush=True)
            else:
                print(f"\033[94m[WebUI]\033[0m \033[91mError: {result.get('error')}\033[0m", flush=True)

    return entry


def should_skip_log():
    return request.headers.get('X-Skip-Log', '0') == '1'


def strip_outer_quotes(value):
    if not isinstance(value, str):
        return value
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


GREP_COMMAND_RE = re.compile(
    r'(?:^|[|;&]\s*)'
    r'(?:sudo(?:\s+-u\s+\S+)?\s+)?'
    r'(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*'
    r'(?:command\s+)?(?:/usr/bin/)?grep\b'
)

SHELL_COMPOSITION_RE = re.compile(r"(?<!\\)(\|\||&&|\|&|[|;<>])")
GLOB_PATTERN_RE = re.compile(r'[*?\[]')


def has_shell_composition(command):
    """Detect shell composition syntax (pipe, redirect, chaining)."""
    if not isinstance(command, str):
        return False
    cmd = command.strip()
    if not cmd:
        return False
    return bool(SHELL_COMPOSITION_RE.search(cmd))


def is_glob_pattern(value):
    if not isinstance(value, str):
        return False
    return bool(GLOB_PATTERN_RE.search(value))


def normalize_data_relative_path(value):
    raw = strip_outer_quotes(value or '').strip()
    if not raw:
        return ''
    raw = raw.replace('\\', '/')
    if raw in ('/hippocamp/data', 'hippocamp/data', 'data', '.', './'):
        return ''
    prefixes = ('/hippocamp/data/', 'hippocamp/data/', 'data/')
    for prefix in prefixes:
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw.startswith('/'):
        raw = raw.lstrip('/')
    return raw.rstrip('/')


def resolve_data_relative_path(value):
    rel_path = normalize_data_relative_path(value)
    data_root = DATA_DIR.resolve()
    target = (data_root / rel_path).resolve()
    try:
        target.relative_to(data_root)
    except ValueError:
        return rel_path, None
    return rel_path, target


def normalize_grep_no_match(command, result, is_error):
    """
    Treat `grep` exit code 1 ("no matches") as non-error in UI logs.
    Keep true command failures (e.g. exit code 2) as errors.
    """
    if not isinstance(result, dict):
        return result, is_error
    if not isinstance(command, str) or not GREP_COMMAND_RE.search(command):
        return result, is_error

    try:
        exit_code = int(result.get('exit_code'))
    except (TypeError, ValueError):
        return result, is_error

    if exit_code != 1:
        return result, is_error

    output_text = result.get('output')
    if isinstance(output_text, str) and output_text.strip():
        # Non-empty output with exit 1 usually means an upstream failure in a pipeline.
        return result, is_error
    if result.get('error'):
        return result, is_error

    normalized = dict(result)
    normalized['success'] = True
    normalized.setdefault('type', 'bash')
    normalized.pop('error', None)
    if not normalized.get('output'):
        normalized['output'] = '(no matches)'
    return normalized, False


def _split_shell_command(command):
    """Split command text while preserving quoted paths."""
    if not isinstance(command, str):
        return []
    try:
        return shlex.split(command)
    except Exception:
        return command.strip().split()


def _extract_api_command(command):
    """
    Extract HippoCamp API command and args from raw shell text.
    Supports direct calls and sudo-prefixed calls.
    """
    tokens = _split_shell_command(command)
    if not tokens:
        return None, []

    candidates = [(tokens[0], tokens[1:])]
    if tokens[0] == 'sudo':
        idx = 1
        while idx < len(tokens):
            token = tokens[idx]
            if token.startswith('-'):
                # Handle options that consume one argument (e.g. -u user)
                if token in ('-u', '-g', '-h', '-p', '-C', '-T', '-R', '-t'):
                    idx += 2
                else:
                    idx += 1
                continue
            break
        if idx < len(tokens):
            candidates.append((tokens[idx], tokens[idx + 1:]))

    valid = {'return_txt', 'txt', 'return_ori', 'ori', 'return_metadata'}
    for raw_cmd, raw_args in candidates:
        cmd = os.path.basename(raw_cmd)
        if cmd in valid:
            return cmd, raw_args
    return None, []


def _find_recent_terminal_error(command, max_age_ms=5000):
    """Try to reuse detailed error already reported by API terminal_notify."""
    now_ms = int(time.time() * 1000)
    for entry in reversed(command_history):
        if entry.get('source') != 'terminal':
            continue
        if entry.get('command') != command:
            continue
        ts_ms = entry.get('ts_ms')
        if isinstance(ts_ms, int) and now_ms - ts_ms > max_age_ms:
            return None
        result = entry.get('result')
        if isinstance(result, dict) and result.get('error'):
            return result.get('error')
    return None


def _probe_api_error(command):
    """
    Best-effort fallback when bash sync only has exit_code.
    Keep this limited to read-only or no-side-effect scenarios.
    """
    cmd, args = _extract_api_command(command)
    if not cmd:
        return None

    if cmd in ('return_txt', 'txt'):
        if not args:
            return 'Usage: return_txt <file_path>'
        result = api_return_txt(strip_outer_quotes(args[0]), _notify=False)
        return result.get('error') if isinstance(result, dict) else None

    if cmd in ('return_ori', 'ori'):
        if not args:
            return 'Usage: return_ori <file_path> [output_path]'
        # Avoid replaying copy behavior for output_path form.
        if len(args) != 1:
            return None
        result = api_return_ori(strip_outer_quotes(args[0]), _notify=False)
        return result.get('error') if isinstance(result, dict) else None

    if cmd == 'return_metadata':
        if not args:
            return 'Usage: return_metadata <file_path>'
        result = api_get_metadata(strip_outer_quotes(args[0]), _notify=False)
        return result.get('error') if isinstance(result, dict) else None

    return None


def build_shell_exec_command(raw_command):
    """
    Wrap a raw shell command with a deterministic bash environment:
    - enable pipefail
    - load aliases from .bashrc
    - provide function shims so API commands in pipes/subshells still run as hippocamp_api
    """
    cmd = str(raw_command or "").strip()
    quoted_cmd = shlex.quote(cmd)
    privileged_fn_shims = (
        'return_txt(){ sudo -u hippocamp_api /hippocamp/api/return_txt "$@"; }; '
        'return_img(){ sudo -u hippocamp_api /hippocamp/api/return_img "$@"; }; '
        'return_ori(){ sudo -u hippocamp_api /hippocamp/api/return_ori "$@"; }; '
        'return_metadata(){ sudo -u hippocamp_api /hippocamp/api/return_metadata "$@"; }; '
        'list_files(){ sudo -u hippocamp_api /hippocamp/api/list_files "$@"; }; '
        'set_flags(){ sudo -u hippocamp_api /hippocamp/api/set_flags "$@"; }; '
        'txt(){ return_txt "$@"; }; '
        'img(){ return_img "$@"; }; '
        'ori(){ return_ori "$@"; }; '
        'ls_files(){ list_files "$@"; }; '
    )
    return (
        "set -o pipefail >/dev/null 2>&1 || true; "
        "shopt -s expand_aliases >/dev/null 2>&1; "
        "source /home/hippocamp_user/.bashrc >/dev/null 2>&1; "
        f"{privileged_fn_shims}"
        f"eval {quoted_cmd}"
    )


def build_return_txt_result(file_path, preview=False, max_chars=None):
    result = api_return_txt(file_path, _notify=False)
    if not preview:
        return result
    if not result.get('success') or 'data' not in result:
        return result
    try:
        full_json = json.dumps(result['data'], ensure_ascii=False, indent=2)
        limit = max_chars if isinstance(max_chars, int) and max_chars > 0 else RETURN_TXT_PREVIEW_MAX
        preview_text = full_json[:limit]
        return {
            'success': True,
            'data_preview': preview_text,
            'truncated': len(full_json) > limit,
            'full_available': True,
            'error': None
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Preview build error: {str(e)}'
        }


def build_return_ori_result(file_path, preview=False, max_chars=None):
    result = api_return_ori(file_path, _notify=False)
    if not preview:
        return result
    if not result.get('success'):
        return result
    file_b64 = result.get('file_b64')
    if not file_b64:
        result['original_path'] = result.get('file_path')
        return result
    try:
        limit = max_chars if isinstance(max_chars, int) and max_chars > 0 else RETURN_ORI_PREVIEW_MAX
        preview_b64 = file_b64[:limit]
        return {
            'success': True,
            'file_path': result.get('file_path'),
            'original_path': result.get('file_path'),
            'file_b64_preview': preview_b64,
            'truncated': len(file_b64) > limit,
            'full_available': True,
            'error': None
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Preview build error: {str(e)}'
        }


def get_file_tree():
    """Get file tree structure for the sidebar"""
    tree = {}

    if not DATA_DIR.exists():
        return tree

    for root, dirs, files in os.walk(DATA_DIR):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        rel_root = os.path.relpath(root, DATA_DIR)
        if rel_root == '.':
            rel_root = ''

        for fname in sorted(files):
            if fname.startswith('.'):
                continue

            if rel_root:
                file_path = f"{rel_root}/{fname}"
            else:
                file_path = fname

            # Build tree structure
            parts = file_path.split('/')
            current = tree
            for i, part in enumerate(parts[:-1]):
                if part not in current:
                    current[part] = {'__is_dir__': True}
                current = current[part]
            current[parts[-1]] = {'__is_file__': True, '__path__': file_path}

    return tree


def tree_to_list(tree, prefix=''):
    """Convert tree dict to flat list for rendering"""
    items = []

    for name, value in sorted(tree.items()):
        if name.startswith('__'):
            continue

        path = f"{prefix}/{name}" if prefix else name

        if value.get('__is_file__'):
            items.append({
                'name': name,
                'path': value['__path__'],
                'type': 'file',
                'ext': Path(name).suffix.lower()
            })
        else:
            # Directory
            children = tree_to_list(value, path)
            items.append({
                'name': name,
                'path': path,
                'type': 'directory',
                'children': children
            })

    return items


def count_files_in_items(items):
    total = 0
    for item in items or []:
        if item.get('type') == 'file':
            total += 1
        else:
            total += count_files_in_items(item.get('children', []))
    return total


def _replay_state_snapshot():
    with replay_state_lock:
        return dict(replay_state)


def _emit_replay_state(snapshot=None):
    payload = snapshot or _replay_state_snapshot()
    socketio.emit('replay_state', payload)
    return payload


def _update_replay_state(**updates):
    with replay_state_lock:
        replay_state.update(updates)
        snapshot = dict(replay_state)
    _emit_replay_state(snapshot)
    return snapshot


def _resolve_replay_events_path(path_value=''):
    raw = str(path_value or '').strip()
    candidates = []
    if raw:
        expanded = Path(raw).expanduser()
        if expanded.is_absolute():
            candidates.append(expanded)
        else:
            try:
                candidates.append((Path(current_directory) / expanded).resolve())
            except Exception:
                pass
            try:
                candidates.append((Path.cwd() / expanded).resolve())
            except Exception:
                pass
            try:
                candidates.append((REPLAY_DEFAULT_EVENTS_PATH.parent / expanded).resolve())
            except Exception:
                pass
    else:
        candidates.append(REPLAY_DEFAULT_EVENTS_PATH)

    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _load_replay_events(events_path):
    with open(events_path, 'r', encoding='utf-8') as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        if isinstance(payload.get('events'), list):
            payload = payload.get('events')
        elif isinstance(payload.get('data'), list):
            payload = payload.get('data')
    if not isinstance(payload, list):
        raise ValueError('events file must be a JSON array')

    indexed_events = []
    for idx, event in enumerate(payload):
        if not isinstance(event, dict):
            continue
        normalized = dict(event)
        try:
            normalized['timestamp'] = float(normalized.get('timestamp', 0.0))
        except Exception:
            normalized['timestamp'] = 0.0
        indexed_events.append((idx, normalized))

    indexed_events.sort(key=lambda row: (row[1].get('timestamp', 0.0), row[0]))
    return [event for _, event in indexed_events]


def _format_event_command(event):
    event_type = str(event.get('event_type', 'event')).strip() or 'event'
    if event_type == 'file_read':
        return f"[replay] file_read {event.get('file_path', '')}"
    if event_type == 'file_write':
        return f"[replay] file_write {event.get('file_path', '')} ({event.get('operation', 'create')})"
    if event_type == 'file_edit':
        return f"[replay] file_edit {event.get('file_path', '')}"
    if event_type == 'file_move':
        return f"[replay] file_move {event.get('old_path', '')} -> {event.get('new_path', '')}"
    if event_type == 'file_rename':
        return f"[replay] file_rename {event.get('old_path', '')} -> {event.get('new_path', '')}"
    if event_type == 'file_copy':
        return f"[replay] file_copy {event.get('source_path', '')} -> {event.get('dest_path', '')}"
    if event_type == 'file_delete':
        return f"[replay] file_delete {event.get('file_path', '')}"
    if event_type == 'file_search':
        return f"[replay] file_search {event.get('search_type', 'glob')} {event.get('query', '')}"
    if event_type == 'file_browse':
        return f"[replay] file_browse {event.get('directory_path', '')}"
    if event_type == 'dir_create':
        return f"[replay] dir_create {event.get('dir_path', '')}"
    if event_type == 'context_switch':
        return f"[replay] context_switch {event.get('from_file', '')} -> {event.get('to_file', '')}"
    if event_type == 'cross_file_reference':
        return f"[replay] cross_file_reference {event.get('source_file', '')} -> {event.get('target_file', '')}"
    if event_type == 'error_encounter':
        return f"[replay] error_encounter {event.get('error_type', '')}"
    if event_type == 'error_response':
        return f"[replay] error_response {event.get('strategy', '')}"
    return f"[replay] {event_type}"


def _wait_for_replay_client(session_id):
    """
    Block replay emission until at least one browser client is connected.
    Returns False if replay session is no longer active.
    If HIPPOCAMP_REPLAY_NO_CLIENT_GATE=1, skip waiting (for headless recording).
    """
    if REPLAY_NO_CLIENT_GATE:
        with replay_state_lock:
            if not replay_state.get('running') or replay_state.get('session_id') != session_id:
                return False
        return True
    while True:
        with replay_state_lock:
            if not replay_state.get('running') or replay_state.get('session_id') != session_id:
                return False
        with replay_clients_cv:
            if replay_connected_clients > 0:
                return True
            replay_clients_cv.wait(timeout=0.25)


def _replay_worker(session_id, events_path, events, speed):
    base_ts = float(events[0].get('timestamp', 0.0)) if events else 0.0
    started = None
    total = len(events)

    for idx, event in enumerate(events):
        with replay_state_lock:
            if not replay_state.get('running') or replay_state.get('session_id') != session_id:
                return

        current_ts = float(event.get('timestamp', base_ts))
        if started is None:
            started = time.perf_counter()
        target_elapsed = max(0.0, (current_ts - base_ts) / max(0.05, float(speed or 1.0)))
        sleep_s = target_elapsed - (time.perf_counter() - started)
        if sleep_s > 0:
            time.sleep(sleep_s)

        with replay_state_lock:
            if not replay_state.get('running') or replay_state.get('session_id') != session_id:
                return
        if not _wait_for_replay_client(session_id):
            return

        payload = {
            'session_id': session_id,
            'index': idx,
            'total': total,
            'event': event,
            'events_path': str(events_path),
            'sent_at': datetime.now(timezone.utc).isoformat()
        }
        socketio.emit('replay_event', payload)
        log_command(
            'agent',
            _format_event_command(event),
            {'success': True, 'event_type': event.get('event_type', 'event')},
            is_error=False
        )
        _update_replay_state(
            events_sent=idx + 1,
            last_event_type=str(event.get('event_type', '')),
            last_event_ts=current_ts
        )

    _update_replay_state(
        running=False,
        completed=True,
        finished_at=datetime.now(timezone.utc).isoformat(),
        error=None
    )


def _start_replay_session(events_path, speed):
    events = _load_replay_events(events_path)
    if not events:
        raise ValueError('events file is empty')

    with replay_state_lock:
        previous_session = int(replay_state.get('session_id') or 0)
        replay_state.update({
            'running': True,
            'completed': False,
            'session_id': previous_session + 1,
            'events_path': str(events_path),
            'speed': max(0.05, float(speed or 1.0)),
            'events_total': len(events),
            'events_sent': 0,
            'last_event_type': '',
            'last_event_ts': None,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'finished_at': None,
            'error': None,
        })
        snapshot = dict(replay_state)

    _emit_replay_state(snapshot)
    thread = threading.Thread(
        target=_replay_worker,
        args=(snapshot['session_id'], events_path, events, snapshot['speed']),
        daemon=True
    )
    thread.start()
    return snapshot


def _stop_replay_session(reason='stopped'):
    with replay_state_lock:
        replay_state['running'] = False
        replay_state['completed'] = False
        replay_state['session_id'] = int(replay_state.get('session_id') or 0) + 1
        replay_state['finished_at'] = datetime.now(timezone.utc).isoformat()
        replay_state['error'] = None if reason in ('stopped', '') else str(reason)
        snapshot = dict(replay_state)
    with replay_clients_cv:
        replay_clients_cv.notify_all()
    _emit_replay_state(snapshot)
    return snapshot


# ==================== Routes ====================

@app.route('/')
def index():
    """Main page"""
    flags = load_feature_flags()
    enable_txt = flags.get('enable_return_txt', True) if flags.get('initialized') else True
    enable_img = flags.get('enable_return_img', True) if flags.get('initialized') else True
    return render_template('index.html',
                         dataset_name=DATASET_NAME,
                         dataset_user=DATASET_USER,
                         flags_initialized=flags.get('initialized', False),
                         enable_return_txt=enable_txt,
                         enable_return_img=enable_img)


@app.route('/api/files')
def get_files():
    """Get all files as tree structure"""
    source = request.headers.get('X-Source', 'api')
    list_path = request.args.get('path', '').strip()
    silent = request.args.get('silent', '0') == '1'
    tree = get_file_tree()
    items = tree_to_list(tree)
    op_files = items
    if list_path:
        parts = [part for part in list_path.split('/') if part]
        current = items
        for part in parts:
            match = None
            for entry in current:
                if entry.get('name') == part and entry.get('type') == 'directory':
                    match = entry.get('children', [])
                    break
            if match is None:
                current = []
                break
            current = match
        op_files = current
    result = {
        'success': True,
        'files': items,
        'total': len(api_list_files()) if callable(api_list_files) else 0
    }
    if result['total'] <= 0:
        result['total'] = count_files_in_items(items)

    # Broadcast list_files operation
    if not silent:
        event = {
            'operation': 'list_files',
            'file_path': list_path,
            'source': source,
            'success': True,
            'files': op_files
        }
        event.update(_timestamp_payload())
        socketio.emit('file_operation', event)

        if not should_skip_log():
            cmd = 'list_files' if not list_path else f"list_files {list_path}"
            log_command(source, cmd, {'success': True, 'count': len(items)})
    return jsonify(result)


@app.route('/api/files/list')
def list_all_files():
    """Get flat list of all files"""
    try:
        pattern = request.args.get('pattern', '').strip()
        files = api_list_files(pattern) if pattern else api_list_files()
        source = request.headers.get('X-Source', 'api')
        if not should_skip_log():
            cmd = 'list_files' if not pattern else f'list_files {pattern}'
            log_command(source, cmd, {'success': True, 'count': len(files)})
        return jsonify({
            'success': True,
            'files': files,
            'total': len(files),
            'pattern': pattern
        })
    except Exception as e:
        source = request.headers.get('X-Source', 'api')
        if not should_skip_log():
            log_command(source, 'list_files', {'success': False, 'error': str(e)}, is_error=True)
        return jsonify({'success': False, 'error': str(e)})


def broadcast_file_operation(operation, file_path, source, result, extra=None):
    """Broadcast file operation to all WebUI clients for UI sync"""
    event_data = {
        'operation': operation,
        'file_path': file_path,
        'source': source,
        'success': result.get('success', False)
    }
    event_data.update(_timestamp_payload())
    if extra:
        event_data.update(extra)
    # Include result data for display
    if result.get('success'):
        if operation == 'return_txt' and 'data_preview' in result:
            event_data['data_preview'] = result.get('data_preview')
            event_data['truncated'] = result.get('truncated', False)
            event_data['full_available'] = result.get('full_available', False)
        elif operation == 'return_txt' and 'data' in result:
            event_data['data'] = result['data']
        elif operation == 'return_img' and 'image_path' in result:
            event_data['image_path'] = result['image_path']
            if 'image_paths' in result:
                event_data['image_paths'] = result['image_paths']
            if 'page_count' in result:
                event_data['page_count'] = result['page_count']
        elif operation == 'return_metadata' and 'metadata' in result:
            event_data['metadata'] = result['metadata']
        elif operation == 'return_ori':
            event_data['original_path'] = result.get('original_path') or result.get('file_path')
            if 'file_b64_preview' in result:
                event_data['file_b64_preview'] = result.get('file_b64_preview')
                event_data['truncated'] = result.get('truncated', False)
                event_data['full_available'] = result.get('full_available', False)
    else:
        event_data['error'] = result.get('error', 'Unknown error')

    socketio.emit('file_operation', event_data)


@app.route('/api/return_txt/<path:file_path>')
def return_txt(file_path):
    """Return gold text for a file"""
    cmd = f"return_txt {file_path}"
    source = request.headers.get('X-Source', 'api')
    preview = request.args.get('preview') == '1'
    try:
        max_chars = int(request.args.get('max_chars', RETURN_TXT_PREVIEW_MAX))
    except Exception:
        max_chars = RETURN_TXT_PREVIEW_MAX
    if re.search(r'(^|\s)--page(\s|$)', file_path):
        error_result = {
            'success': False,
            'error': 'return_txt does not support --page; use return_img <file_path> --page N'
        }
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation('return_txt', file_path, source, error_result)
        return jsonify(error_result)

    flags = load_feature_flags()
    if flags.get('initialized') and not flags.get('enable_return_txt'):
        error_result = {'success': False, 'error': 'return_txt is disabled by feature flags'}
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation('return_txt', file_path, source, error_result)
        return jsonify(error_result)

    try:
        result = build_return_txt_result(file_path, preview=preview, max_chars=max_chars)
        if not should_skip_log():
            log_command(source, cmd, result, is_error=not result.get('success'))
        broadcast_file_operation('return_txt', file_path, source, result)
        return jsonify(result)
    except Exception as e:
        error_result = {'success': False, 'error': str(e)}
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation('return_txt', file_path, source, error_result)
        return jsonify(error_result)


@app.route('/api/return_txt_full/<path:file_path>')
def return_txt_full(file_path):
    """Return full text JSON for a file (no preview)"""
    flags = load_feature_flags()
    if flags.get('initialized') and not flags.get('enable_return_txt'):
        return jsonify({'success': False, 'error': 'return_txt is disabled by feature flags'})
    try:
        result = api_return_txt(file_path, _notify=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/return_img/<path:file_path>')
def return_img(file_path):
    """Convert file to image and return path"""
    cmd = f"return_img {file_path}"
    source = request.headers.get('X-Source', 'api')
    page = request.args.get('page')
    flags = load_feature_flags()
    if flags.get('initialized') and not flags.get('enable_return_img'):
        error_result = {'success': False, 'error': 'return_img is disabled by feature flags'}
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation('return_img', file_path, source, error_result)
        return jsonify(error_result)

    try:
        result = api_return_img(file_path, _notify=False, page=page)
        if not should_skip_log():
            log_command(source, cmd, result, is_error=not result.get('success'))
        broadcast_file_operation('return_img', file_path, source, result)
        return jsonify(result)
    except Exception as e:
        error_result = {'success': False, 'error': str(e)}
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation('return_img', file_path, source, error_result)
        return jsonify(error_result)


@app.route('/api/return_ori/<path:file_path>')
def return_ori(file_path):
    """Return original file path"""
    cmd = f"return_ori {file_path}"
    source = request.headers.get('X-Source', 'api')
    preview = request.args.get('preview') == '1'
    try:
        max_chars = int(request.args.get('max_chars', RETURN_ORI_PREVIEW_MAX))
    except Exception:
        max_chars = RETURN_ORI_PREVIEW_MAX

    try:
        result = build_return_ori_result(file_path, preview=preview, max_chars=max_chars)
        if not should_skip_log():
            log_command(source, cmd, result, is_error=not result.get('success'))
        broadcast_file_operation('return_ori', file_path, source, result)
        return jsonify(result)
    except Exception as e:
        error_result = {'success': False, 'error': str(e)}
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation('return_ori', file_path, source, error_result)
        return jsonify(error_result)


@app.route('/api/return_ori_full/<path:file_path>')
def return_ori_full(file_path):
    """Return full ori JSON for a file (no preview)"""
    try:
        result = api_return_ori(file_path, _notify=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def _handle_metadata_request(file_path, cmd_name, op_name):
    source = request.headers.get('X-Source', 'api')
    cmd = f"{cmd_name} {file_path}"
    try:
        result = api_get_metadata(file_path, _notify=False)
        if not should_skip_log():
            log_command(source, cmd, result, is_error=not result.get('success'))
        broadcast_file_operation(op_name, file_path, source, result)
        return jsonify(result)
    except Exception as e:
        error_result = {'success': False, 'error': str(e)}
        if not should_skip_log():
            log_command(source, cmd, error_result, is_error=True)
        broadcast_file_operation(op_name, file_path, source, error_result)
        return jsonify(error_result)


@app.route('/api/return_metadata/<path:file_path>')
def return_metadata(file_path):
    """Return metadata for a file"""
    return _handle_metadata_request(file_path, 'return_metadata', 'return_metadata')




@app.route('/api/serve_image/<path:image_path>')
def serve_image(image_path):
    """Serve a generated image from output directory"""
    # Handle URL-decoded paths (Flask strips leading /)
    # e.g., "hippocamp/output/file.png" -> "/hippocamp/output/file.png"
    if image_path.startswith('hippocamp/'):
        full_path = Path('/') / image_path
    elif image_path.startswith('/'):
        full_path = Path(image_path)
    else:
        # Try output directory first
        full_path = OUTPUT_DIR / image_path
        if not full_path.exists():
            # Try as absolute path with leading /
            full_path = Path('/' + image_path)

    if full_path.exists():
        return send_file(full_path)

    return jsonify({'success': False, 'error': f'Image not found: {image_path}'}), 404


@app.route('/api/serve_file/<path:file_path>')
def serve_file(file_path):
    """Serve any file from data directory"""
    full_path = DATA_DIR / file_path

    if full_path.exists():
        return send_file(full_path)

    return jsonify({'success': False, 'error': 'File not found'}), 404


@app.route('/api/history')
def get_history():
    """Get command history"""
    return jsonify({
        'success': True,
        'history': command_history
    })


@app.route('/api/ui_ops', methods=['POST'])
def ui_ops():
    """Persist UI-only virtual file operations into /tmp during this run."""
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', '')
        payload = data.get('payload', {})
        result = append_tmp_ui_event(action, payload)
        if result.get('success'):
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': result.get('error', 'append failed')}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/replay/status')
def replay_status():
    return jsonify({'success': True, 'state': _replay_state_snapshot()})


@app.route('/api/replay/start', methods=['POST'])
def replay_start():
    data = request.get_json(silent=True) or {}
    events_path_raw = str(data.get('path', '') or '').strip()
    speed_raw = data.get('speed', REPLAY_DEFAULT_SPEED)
    try:
        speed = max(0.05, float(speed_raw))
    except Exception:
        speed = REPLAY_DEFAULT_SPEED

    events_path = _resolve_replay_events_path(events_path_raw)
    if events_path is None:
        fallback = events_path_raw or str(REPLAY_DEFAULT_EVENTS_PATH)
        return jsonify({'success': False, 'error': f'events file not found: {fallback}'}), 404

    if _replay_state_snapshot().get('running'):
        _stop_replay_session('restarted')

    try:
        snapshot = _start_replay_session(events_path, speed)
        return jsonify({'success': True, 'state': snapshot})
    except Exception as e:
        _update_replay_state(
            running=False,
            completed=False,
            events_path=str(events_path),
            error=str(e),
            finished_at=datetime.now(timezone.utc).isoformat()
        )
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/replay/stop', methods=['POST'])
def replay_stop():
    snapshot = _stop_replay_session('stopped')
    return jsonify({'success': True, 'state': snapshot})


@app.route('/api/replay/read_text', methods=['POST'])
def replay_read_text():
    data = request.get_json(silent=True) or {}
    rel_path = str(data.get('path', '') or '').strip()
    events_path_raw = str(data.get('events_path', '') or '').strip()
    max_chars_raw = data.get('max_chars', 2000000)

    if not rel_path:
        return jsonify({'success': False, 'error': 'missing path'}), 400

    try:
        max_chars = max(1, int(max_chars_raw))
    except Exception:
        max_chars = 2000000

    candidate = Path(rel_path).expanduser()
    if not candidate.is_absolute():
        events_path = _resolve_replay_events_path(events_path_raw)
        base_dir = events_path.parent if events_path else REPLAY_DEFAULT_EVENTS_PATH.parent
        candidate = (base_dir / rel_path).resolve()
    else:
        candidate = candidate.resolve()

    if not candidate.exists() or not candidate.is_file():
        return jsonify({'success': False, 'error': f'file not found: {candidate}'}), 404

    try:
        with open(candidate, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(max_chars + 1)
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]
        return jsonify({
            'success': True,
            'path': str(candidate),
            'content': content,
            'truncated': truncated
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/replay/write_file', methods=['POST'])
def replay_write_file():
    data = request.get_json(silent=True) or {}
    file_path = normalize_data_relative_path(data.get('file_path', ''))
    content = data.get('content', '')
    operation = str(data.get('operation', 'overwrite') or 'overwrite').strip().lower()

    if not file_path:
        return jsonify({'success': False, 'error': 'missing file_path'}), 400

    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False, indent=2)
        except Exception:
            content = str(content)

    _, target = resolve_data_relative_path(file_path)
    if target is None:
        return jsonify({'success': False, 'error': f'invalid file_path: {file_path}'}), 400

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if operation == 'append':
            with open(target, 'a', encoding='utf-8') as f:
                f.write(content)
        else:
            with open(target, 'w', encoding='utf-8') as f:
                f.write(content)
        return jsonify({
            'success': True,
            'file_path': file_path,
            'full_path': str(target),
            'content_length': len(content)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/terminal_notify', methods=['POST'])
def terminal_notify():
    """Receive command notification from terminal (HTTP POST)"""
    try:
        data = request.get_json()
        if data:
            command = data.get('command', '')
            result = data.get('result')
            is_error = data.get('is_error', False)
            result, is_error = normalize_grep_no_match(command, result, is_error)
            ts = data.get('timestamp')
            ts_ms = data.get('ts_ms')

            entry = {
                'source': 'terminal',
                'command': command,
                'result': result,
                'is_error': is_error
            }
            if ts is not None:
                entry['timestamp'] = ts
            if ts_ms is not None:
                entry['ts_ms'] = ts_ms
            if 'timestamp' not in entry or 'ts_ms' not in entry:
                entry.update(_timestamp_payload())
            command_history.append(entry)
            if len(command_history) > MAX_HISTORY:
                command_history.pop(0)

            # Broadcast to WebUI via WebSocket
            socketio.emit('command_executed', entry)

            return jsonify({'success': True})
    except Exception as e:
        pass
    return jsonify({'success': False}), 400


@app.route('/api/log_command', methods=['POST'])
def log_command_api():
    """Receive command notification from agent or external client"""
    try:
        data = request.get_json() or {}
        command = data.get('command', '')
        source = data.get('source', 'agent')
        result = data.get('result')
        is_error = data.get('is_error', False)
        result, is_error = normalize_grep_no_match(command, result, is_error)
        if not command:
            return jsonify({'success': False, 'error': 'Missing command'}), 400
        log_command(source, command, result, is_error)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/feature_flags', methods=['GET', 'POST'])
def feature_flags():
    """Feature flags (read-only)."""
    if request.method == 'GET':
        flags = load_feature_flags()
        return jsonify({
            'success': True,
            'initialized': flags.get('initialized', False),
            'enable_return_txt': flags.get('enable_return_txt', True),
            'enable_return_img': flags.get('enable_return_img', True),
            'locked': flags.get('initialized', False)
        })
    return jsonify({
        'success': False,
        'error': 'feature_flags is read-only.',
        'locked': True
    }), 403


@app.route('/api/bash_notify', methods=['POST'])
def bash_notify():
    """Receive bash command notification (for ls, cat, etc.)"""
    try:
        data = request.get_json()
        if data:
            command = data.get('command', '')
            exit_code = data.get('exit_code', 0)
            result = {'success': exit_code == 0, 'exit_code': exit_code, 'type': 'bash'}
            is_error = exit_code != 0

            if is_error:
                detailed_error = _find_recent_terminal_error(command)
                if not detailed_error:
                    detailed_error = _probe_api_error(command)
                if detailed_error:
                    result['error'] = detailed_error

            result, is_error = normalize_grep_no_match(command, result, is_error)

            entry = {
                'source': 'terminal',
                'command': command,
                'result': result,
                'is_error': is_error
            }
            entry.update(_timestamp_payload())
            command_history.append(entry)
            if len(command_history) > MAX_HISTORY:
                command_history.pop(0)

            # Broadcast to WebUI
            socketio.emit('command_executed', entry)

            # Also broadcast as file_operation for UI updates (ls, cd, cat, etc.)
            broadcast_bash_operation(command, result)

            return jsonify({'success': True})
    except:
        pass
    return jsonify({'success': False}), 400


# ==================== WebSocket Events ====================

@socketio.on('connect')
def handle_connect():
    """Handle WebUI client connection"""
    global replay_connected_clients
    with replay_clients_cv:
        replay_connected_clients += 1
        replay_clients_cv.notify_all()
    emit('connected', {
        'dataset': DATASET_NAME,
        'user': DATASET_USER,
        'history': command_history[-20:],  # Send last 20 commands
        'current_directory': current_directory
    })
    emit('replay_state', _replay_state_snapshot())


@socketio.on('disconnect')
def handle_disconnect():
    """Track WebUI client disconnects for replay gating."""
    global replay_connected_clients
    with replay_clients_cv:
        replay_connected_clients = max(0, replay_connected_clients - 1)
        replay_clients_cv.notify_all()


def broadcast_bash_operation(command, result):
    """Broadcast bash command for UI sync"""
    parts = command.strip().split()
    if not parts:
        return

    cmd_name = parts[0]
    cmd_args = parts[1:] if len(parts) > 1 else []

    # Map bash commands to UI operations
    ui_operations = {
        'ls': 'list_files',
        'dir': 'list_files',
        'cat': 'view_file',
        'head': 'view_file',
        'tail': 'view_file',
        'less': 'view_file',
        'more': 'view_file',
        'cd': 'change_dir',
        'pwd': 'show_path',
        'file': 'file_info',
        'stat': 'file_info',
    }

    if cmd_name in ui_operations:
        # Extract file/directory path from args
        file_path = ''
        for arg in cmd_args:
            if not arg.startswith('-'):
                file_path = arg.strip('"\'')
                break

        # Clean path
        clean_path = file_path.replace('/hippocamp/data/', '').replace('data/', '').lstrip('./')

        event_data = {
            'operation': ui_operations[cmd_name],
            'bash_command': cmd_name,
            'file_path': clean_path,
            'source': 'terminal',
            'success': result.get('success', False)
        }
        event_data.update(_timestamp_payload())

        # Include output for view operations
        if cmd_name in ['cat', 'head', 'tail', 'less', 'more'] and result.get('output'):
            event_data['content'] = result.get('output', '')[:5000]  # Limit size

        socketio.emit('file_operation', event_data)


@socketio.on('execute_command')
def handle_execute_command(data):
    """Execute a command from WebUI and sync to terminal"""
    global current_directory

    command = data.get('command', '').strip()

    if not command:
        return

    # Parse and execute the command
    parts = command.split(maxsplit=1)
    cmd_name = parts[0]
    cmd_arg = parts[1] if len(parts) > 1 else ''
    contains_shell_composition = has_shell_composition(command)

    result = None
    is_error = False

    try:
        if result is None:
            # HippoCamp API commands
            if cmd_name == 'return_txt' and not contains_shell_composition:
                if re.search(r'(^|\s)--page(\s|$)', cmd_arg):
                    result = {
                        'success': False,
                        'output': 'return_txt does not support --page; use return_img <file_path> --page N',
                        'error': 'return_txt does not support --page; use return_img <file_path> --page N',
                        'type': 'bash'
                    }
                    is_error = True
                    broadcast_file_operation('return_txt', cmd_arg, 'webui', result)
                else:
                    file_arg = strip_outer_quotes(cmd_arg)
                    result = build_return_txt_result(file_arg, preview=True, max_chars=RETURN_TXT_PREVIEW_MAX)
                    broadcast_file_operation('return_txt', cmd_arg, 'webui', result)
            elif cmd_name == 'return_img' and not contains_shell_composition:
                file_arg = strip_outer_quotes(cmd_arg)
                result = api_return_img(file_arg, _notify=False, allow_disabled=True)
                broadcast_file_operation('return_img', cmd_arg, 'webui', result)
            elif cmd_name == 'return_ori' and not contains_shell_composition:
                file_arg = strip_outer_quotes(cmd_arg)
                result = build_return_ori_result(file_arg, preview=True, max_chars=RETURN_ORI_PREVIEW_MAX)
                broadcast_file_operation('return_ori', cmd_arg, 'webui', result)
            elif cmd_name == 'return_metadata' and not contains_shell_composition:
                file_arg = strip_outer_quotes(cmd_arg)
                result = api_get_metadata(file_arg, _notify=False)
                broadcast_file_operation('return_metadata', cmd_arg, 'webui', result)
            elif cmd_name == 'list_files' and not contains_shell_composition:
                list_arg = strip_outer_quotes(cmd_arg) if cmd_arg else ''
                normalized_arg = normalize_data_relative_path(list_arg)

                if not normalized_arg:
                    files = api_list_files('', _notify=False)
                    result = {
                        'success': True,
                        'files': files,
                        'total': len(files),
                        'path_exists': True,
                        'path_type': 'directory'
                    }
                elif is_glob_pattern(normalized_arg):
                    files = api_list_files(normalized_arg, _notify=False)
                    result = {
                        'success': True,
                        'files': files,
                        'total': len(files),
                        'pattern': normalized_arg
                    }
                else:
                    rel_path, abs_path = resolve_data_relative_path(normalized_arg)
                    if abs_path is None or not abs_path.exists():
                        result = {
                            'success': False,
                            'error': f'路径不存在: {normalized_arg}',
                            'files': [],
                            'total': 0,
                            'path_exists': False
                        }
                        is_error = True
                    elif abs_path.is_file():
                        result = {
                            'success': True,
                            'files': [rel_path],
                            'total': 1,
                            'path': rel_path,
                            'path_exists': True,
                            'path_type': 'file'
                        }
                    else:
                        files = api_list_files(f"{rel_path}/*", _notify=False) if rel_path else api_list_files('', _notify=False)
                        result = {
                            'success': True,
                            'files': files,
                            'total': len(files),
                            'path': rel_path,
                            'path_exists': True,
                            'path_type': 'directory'
                        }
            elif cmd_name == 'hhelp' or cmd_name == 'hippocamp_help':
                try:
                    proc = subprocess.run(
                        'python3 /hippocamp/api/hippocamp_help',
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        cwd=current_directory
                    )
                    output = proc.stdout
                    if proc.stderr:
                        output += ('\n' if output else '') + proc.stderr
                    result = {
                        'success': proc.returncode == 0,
                        'output': output or '(no output)',
                        'exit_code': proc.returncode,
                        'type': 'bash'
                    }
                    is_error = proc.returncode != 0
                except subprocess.TimeoutExpired:
                    result = {'success': False, 'error': 'Command timed out (10s)', 'type': 'bash'}
                    is_error = True
                except Exception as e:
                    result = {'success': False, 'error': str(e), 'type': 'bash'}
                    is_error = True
            elif cmd_name == 'set_flags':
                args = cmd_arg.split()
                if len(args) != 2 or args[0] not in ('0', '1') or args[1] not in ('0', '1'):
                    result = {
                        'success': False,
                        'output': 'Usage: set_flags <return_txt 0|1> <return_img 0|1>',
                        'error': 'invalid set_flags arguments',
                        'type': 'bash'
                    }
                    is_error = True
                else:
                    result = init_feature_flags(args[0] == '1', args[1] == '1')
                    if not result.get('success'):
                        is_error = True
            elif cmd_name == 'cd':
                # Special handling for cd - update persistent working directory
                target = cmd_arg.strip() if cmd_arg else '/hippocamp/data'

                # Handle special paths
                if target == '~' or target == '':
                    target = '/hippocamp/data'
                elif target == '-':
                    target = '/hippocamp/data'  # No previous dir tracking yet
                elif target == '..':
                    target = os.path.dirname(current_directory.rstrip('/')) or '/'
                elif not target.startswith('/'):
                    # Relative path - join with current directory
                    target = os.path.normpath(os.path.join(current_directory, target))

                # Enforce data-root restriction
                if not target.startswith('/hippocamp/data'):
                    result = {
                        'success': False,
                        'output': "cd: permission denied: outside /hippocamp/data",
                        'error': "Access restricted to /hippocamp/data",
                        'type': 'bash'
                    }
                    is_error = True
                elif os.path.isdir(target):
                    current_directory = target
                    result = {
                        'success': True,
                        'output': f'Changed to: {target}',
                        'type': 'bash',
                        'new_directory': target
                    }
                    # Broadcast directory change to update UI
                    clean_path = target.replace('/hippocamp/data/', '').replace('/hippocamp/', '').lstrip('/')
                    socketio.emit('file_operation', {
                        'operation': 'change_dir',
                        'file_path': clean_path,
                        'full_path': target,
                        'source': 'webui',
                        'success': True,
                        **_timestamp_payload()
                    })
                else:
                    result = {
                        'success': False,
                        'output': f"cd: can't cd to {cmd_arg}: No such directory",
                        'error': f"No such directory: {cmd_arg}",
                        'type': 'bash'
                    }
                    is_error = True
            else:
                # Execute as bash command
                try:
                    bash_cmd = build_shell_exec_command(command)
                    proc = subprocess.run(
                        ["/bin/bash", "-lc", bash_cmd],
                        shell=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        cwd=current_directory
                    )
                    output = proc.stdout
                    if proc.stderr:
                        output += ('\n' if output else '') + proc.stderr
                    result = {
                        'success': proc.returncode == 0,
                        'output': output or '(no output)',
                        'exit_code': proc.returncode,
                        'type': 'bash'
                    }
                    is_error = proc.returncode != 0

                    # Broadcast bash operation for UI sync
                    broadcast_bash_operation(command, result)

                except subprocess.TimeoutExpired:
                    result = {'success': False, 'error': 'Command timed out (30s)', 'type': 'bash'}
                    is_error = True
                except Exception as e:
                    result = {'success': False, 'error': str(e), 'type': 'bash'}
                    is_error = True
    except Exception as e:
        result = {'success': False, 'error': str(e)}
        is_error = True

    result, is_error = normalize_grep_no_match(command, result, is_error)

    # Log and broadcast (log_command also prints to terminal)
    entry = log_command('webui', command, result, is_error)


@socketio.on('terminal_command')
def handle_terminal_command(data):
    """Receive command notification from terminal"""
    command = data.get('command', '')
    result = data.get('result')
    is_error = data.get('is_error', False)
    result, is_error = normalize_grep_no_match(command, result, is_error)

    log_command('terminal', command, result, is_error)


# ==================== Terminal Integration ====================

# FIFO pipe for terminal -> WebUI communication
COMMAND_PIPE = '/hippocamp/output/.webui/hippocamp_commands'

def setup_command_pipe():
    """Create FIFO pipe for terminal communication"""
    try:
        if os.path.exists(COMMAND_PIPE):
            os.remove(COMMAND_PIPE)
        os.mkfifo(COMMAND_PIPE)
    except Exception as e:
        print(f"Warning: Could not create command pipe: {e}")


def watch_terminal_commands():
    """Watch for commands from terminal"""
    global terminal_watcher_running
    terminal_watcher_running = True

    while terminal_watcher_running:
        try:
            if os.path.exists(COMMAND_PIPE):
                with open(COMMAND_PIPE, 'r') as pipe:
                    for line in pipe:
                        line = line.strip()
                        if line:
                            try:
                                data = json.loads(line)
                                command = data.get('command', '')
                                result = data.get('result')
                                is_error = data.get('is_error', False)
                                result, is_error = normalize_grep_no_match(command, result, is_error)
                                log_command(
                                    'terminal',
                                    command,
                                    result,
                                    is_error
                                )
                            except json.JSONDecodeError:
                                # Plain text command
                                log_command('terminal', line)
        except Exception as e:
            time.sleep(1)


def start_terminal_watcher():
    """Start the terminal watcher thread"""
    setup_command_pipe()
    thread = threading.Thread(target=watch_terminal_commands, daemon=True)
    thread.start()
    return thread


def maybe_autostart_replay():
    """Auto-start replay if enabled via environment."""
    if not REPLAY_AUTOSTART:
        return
    events_path = _resolve_replay_events_path(str(REPLAY_DEFAULT_EVENTS_PATH))
    if events_path is None:
        print(f"Warning: replay autostart skipped, file not found: {REPLAY_DEFAULT_EVENTS_PATH}", flush=True)
        return

    def _runner():
        time.sleep(1.0)
        try:
            _start_replay_session(events_path, REPLAY_DEFAULT_SPEED)
            print(
                f"[Replay] Autostart enabled: {events_path} (speed={REPLAY_DEFAULT_SPEED})",
                flush=True
            )
        except Exception as e:
            _update_replay_state(
                running=False,
                completed=False,
                events_path=str(events_path),
                error=str(e),
                finished_at=datetime.now(timezone.utc).isoformat()
            )
            print(f"[Replay] Autostart failed: {e}", flush=True)

    threading.Thread(target=_runner, daemon=True).start()


# ==================== Main ====================

if __name__ == '__main__':
    reset_tmp_ui_state()

    # Start terminal watcher
    start_terminal_watcher()
    maybe_autostart_replay()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║                   HippoCamp WebUI                            ║
║                  Dataset: {DATASET_NAME:<20}              ║
║                  User: {DATASET_USER:<24}              ║
╚══════════════════════════════════════════════════════════════╝

WebUI running at: http://localhost:{WEBUI_PORT}

Terminal commands will be displayed in WebUI.
WebUI actions will be displayed in Terminal.
    """)

    socketio.run(app, host='0.0.0.0', port=WEBUI_PORT, debug=False, allow_unsafe_werkzeug=True)
