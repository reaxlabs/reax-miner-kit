"""Retired Mammoth Triton seed preflight. New installs no longer use it; kept for installs rendered 4 Nov 2026."""
import hashlib
from pathlib import Path

SHA256 = '51b996aa4e577fa923c4d6142b79c613a9aba2e79506dff111277ba8e8b2a326'
CACHE_HASH = 'UBI36QQLJDLOXDYUOXUAFH7IJLNLP3IQAUE3ETMXLNEPEZIFLWYQ'
FILENAME = CACHE_HASH + '/chunk_fwd_kernel_o.autotune.json'
DIRECTORY = Path(__file__).resolve().parent.parent / 'configs/mammoth-triton-seed'
ENTRYPOINT = ['/bin/sh', '-c', 'mkdir -p /tmp/triton && cp -r /seedctl/. /tmp/triton/ || exit 97; exec "$0" "$@"', 'python', '-m', 'engine.serve']


def applies(pool):
    return bool(pool and pool['id'] == 's1-pro' and
                (pool.get('runtime') or {}).get('contract') == 'mammoth-reference/1')


def inspect():
    path = DIRECTORY / FILENAME
    present = path.is_file()
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if present else None
    return dict(path=str(path), present=present, sha256=actual, expected_sha256=SHA256,
                ok=present and not any(p.is_symlink() for p in (DIRECTORY, path.parent, path)) and actual == SHA256)


def verify():
    try:
        info = inspect()
    except OSError as exc:
        raise ValueError('Mammoth Triton seed unreadable; reinstall the kit.') from exc
    if not info['ok']:
        raise ValueError('Mammoth Triton seed missing or checksum mismatch (expected SHA256 ' + SHA256 + '); reinstall the kit before starting.')
    return DIRECTORY


if __name__ == '__main__':
    # A read-only preflight for manual compose/systemd setups, no Docker or GPU.
    try:
        verify()
    except ValueError as exc:
        raise SystemExit(str(exc))
    print('Mammoth Triton seed present; SHA256 ' + SHA256)
