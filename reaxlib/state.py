"""State paths and private, atomic JSON persistence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def home(args=None):
    override = args if isinstance(args, (str, Path)) else getattr(args, 'home', None)
    path = Path(override or os.environ.get('REAX_HOME') or '~/.reax-miner').expanduser()
    if path.is_symlink():
        raise ValueError('State directory must not be a symbolic link')
    path = path.absolute()
    if any(c in str(path) for c in ('\n', '\r', '\0')):
        raise ValueError('State paths cannot contain control characters')
    return path


def read_json(path, default=None):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            value = json.load(stream)
        return value
    except (OSError, ValueError):
        return {} if default is None else default


def reject_symlinks(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path,) + tuple(path.parents)):
        raise ValueError('Path must not traverse symbolic links')


def private_dir(path):
    path = Path(path)
    reject_symlinks(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def child_env(source=None):
    source = os.environ if source is None else source
    names = {'PATH', 'HOME', 'LANG', 'TMPDIR', 'DOCKER_HOST', 'HF_HOME', 'HF_ENDPOINT',
             'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
             'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
    return {k: v for k, v in source.items() if k in names or k.startswith(('LC_', 'XDG_', 'REAX_'))}


def write_text(path, value, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('File target must not be a symbolic link')
    reject_symlinks(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise ValueError("Parent directory must not be a symbolic link")
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, value, mode=0o600):
    write_text(path, json.dumps(value, indent=2) + '\n', mode)


def load_config(args=None):
    value = read_json(home(args) / 'config.json')
    return value if isinstance(value, dict) else {}


def save_config(args, value):
    private_dir(home(args))
    write_json(home(args) / 'config.json', value)


def load_state(args=None):
    value = read_json(home(args) / 'state.json')
    return value if isinstance(value, dict) else {}


def save_state(args, value):
    private_dir(home(args))
    write_json(home(args) / 'state.json', value)


def mode(args=None):
    return getattr(args, 'mode', None) or load_config(args).get('mode', 'localnet')
