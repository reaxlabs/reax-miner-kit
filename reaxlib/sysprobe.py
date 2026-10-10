"""All subprocess probing passes through run; facts is injectable too."""
from __future__ import annotations

import os
import platform
from pathlib import Path
import shutil
import socket
import subprocess
import sys


def run(cmd, timeout=0.5):
    try:
        from .state import child_env
        proc = subprocess.run(cmd, env=child_env(), capture_output=True, text=True, timeout=timeout)
        return {'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'returncode': 127, 'stdout': '', 'stderr': str(exc)}


def facts(home, port=8091):
    proc_version = ''
    try:
        proc_version = Path('/proc/version').read_text().lower()
    except OSError:
        pass
    os_name = 'wsl2' if 'microsoft' in proc_version else {'linux': 'linux', 'darwin': 'macos', 'win32': 'windows-native'}.get(sys.platform, sys.platform)
    ram = None
    try:
        ram = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024 ** 3
    except (OSError, ValueError, AttributeError):
        pass
    disk_path = Path(home)
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    disk = shutil.disk_usage(str(disk_path)).free / 1024 ** 3
    free = False
    try:
        with socket.socket() as sock:
            sock.bind(('0.0.0.0', port))
            free = True
    except OSError:
        pass
    groups = []
    try:
        import grp
        groups = [grp.getgrgid(gid).gr_name for gid in os.getgroups()]
    except (ImportError, OSError, KeyError):
        pass
    return {'os': os_name, 'arch': platform.machine(), 'cpu_cores': os.cpu_count() or 1, 'ram_gb': ram,
            'disk_free_gb': disk, 'python_version': platform.python_version(), 'groups': groups,
            'systemd_available': Path('/run/systemd/system').exists(), 'miner_port_free': free,
            'wsl_gpu_path': Path('/usr/lib/wsl/lib').exists()}
