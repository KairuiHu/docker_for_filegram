#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Terminal Sync - Wrapper to sync terminal commands with WebUI

This module provides functions that wrap the hippocamp API calls
and send notifications to the WebUI for real-time sync.

Usage:
    source /hippocamp/webui/terminal_sync.sh
    # Now all API commands will sync with WebUI
"""
import os
import sys
import json
from datetime import datetime

# FIFO pipe path for communication with WebUI
COMMAND_PIPE = '/hippocamp/output/.webui/hippocamp_commands'


def notify_webui(command, result=None, is_error=False):
    """Send command notification to WebUI via FIFO pipe"""
    try:
        if os.path.exists(COMMAND_PIPE):
            entry = {
                'command': command,
                'result': result,
                'is_error': is_error,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
            with open(COMMAND_PIPE, 'w') as pipe:
                pipe.write(json.dumps(entry) + '\n')
    except Exception as e:
        # Silently fail if WebUI is not running
        pass


def sync_return_txt(file_path):
    """Wrapper for return_txt that syncs with WebUI"""
    sys.path.insert(0, '/hippocamp/api')
    from hippocamp_api import return_txt

    cmd = f"return_txt {file_path}"
    result = return_txt(file_path)
    notify_webui(cmd, result, is_error=not result.get('success'))
    return result


def sync_return_img(file_path, output_path=None):
    """Wrapper for return_img that syncs with WebUI"""
    sys.path.insert(0, '/hippocamp/api')
    from hippocamp_api import return_img

    cmd = f"return_img {file_path}"
    if output_path:
        cmd += f" {output_path}"

    result = return_img(file_path, output_path)
    notify_webui(cmd, result, is_error=not result.get('success'))
    return result


def sync_return_ori(file_path, output_path=None):
    """Wrapper for return_ori that syncs with WebUI"""
    sys.path.insert(0, '/hippocamp/api')
    from hippocamp_api import return_ori

    cmd = f"return_ori {file_path}"
    if output_path:
        cmd += f" {output_path}"

    result = return_ori(file_path, output_path)
    notify_webui(cmd, result, is_error=not result.get('success'))
    return result


def sync_return_metadata(file_path, cmd_name="return_metadata"):
    """Wrapper for return_metadata that syncs with WebUI"""
    sys.path.insert(0, '/hippocamp/api')
    from hippocamp_api import get_metadata

    cmd = f"{cmd_name} {file_path}"
    result = get_metadata(file_path)
    notify_webui(cmd, result, is_error=not result.get('success'))
    return result


def sync_list_files(pattern=None):
    """Wrapper for list_files that syncs with WebUI"""
    sys.path.insert(0, '/hippocamp/api')
    from hippocamp_api import list_files

    cmd = "list_files"
    if pattern:
        cmd += f" {pattern}"

    try:
        files = list_files()
        if pattern:
            import fnmatch
            if pattern.endswith('/'):
                files = [f for f in files if f.startswith(pattern)]
            else:
                files = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(os.path.basename(f), pattern)]

        result = {'success': True, 'files': files, 'total': len(files)}
        notify_webui(cmd, result)
        return result
    except Exception as e:
        result = {'success': False, 'error': str(e)}
        notify_webui(cmd, result, is_error=True)
        return result


if __name__ == '__main__':
    # Test
    print("Terminal sync module loaded.")
    print(f"FIFO pipe: {COMMAND_PIPE}")
