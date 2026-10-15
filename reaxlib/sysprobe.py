"""All subprocess probing passes through run; facts is injectable too."""
from __future__ import annotations

import os
import platform
import re
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


def detect_jetson(release_file='/etc/nv_tegra_release', model_file='/proc/device-tree/model'):
    """NVIDIA Jetson facts ({'l4t': '36.4.0', 'model': ...}) or None on any other machine."""
    l4t, model = None, None
    try:
        text = Path(release_file).read_text()
        match = re.search(r'#\s*R(\d+)\s*\(release\),\s*REVISION:\s*(\d+(?:\.\d+)*)', text)
        if match:
            l4t = match.group(1) + '.' + match.group(2)
    except OSError:
        pass
    try:
        model = Path(model_file).read_bytes().split(b'\0')[0].decode('utf-8', 'replace').strip()
    except OSError:
        pass
    if l4t is None and not (model and 'jetson' in model.lower()):
        return None
    return {'l4t': l4t, 'model': model or None}


def normalized_arch(arch):
    return {'amd64': 'x86_64', 'x86_64': 'x86_64', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(str(arch).lower(), str(arch).lower())


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
    try:
        import importlib.util
        venv_ok = importlib.util.find_spec('ensurepip') is not None  # Debian/Ubuntu ship it separately as python3-venv
    except (ImportError, ValueError):
        venv_ok = False
    import tarfile
    return {'os': os_name, 'arch': platform.machine(), 'cpu_cores': os.cpu_count() or 1, 'ram_gb': ram,
            'disk_free_gb': disk, 'python_version': platform.python_version(), 'python_venv': venv_ok,
            'python_tar_filter': hasattr(tarfile, 'data_filter'), 'groups': groups,
            'systemd_available': Path('/run/systemd/system').exists(), 'miner_port_free': free,
            'wsl_gpu_path': Path('/usr/lib/wsl/lib').exists(), 'jetson': detect_jetson()}
