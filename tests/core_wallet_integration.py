"""Run manually in a throwaway venv with bittensor==11.1.0 and optional bittensor-wallet."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reaxlib import ss58
from reaxlib.cmd_wallet import BACKEND_SCRIPT, coldkeypub, hotkey
from reaxlib import sysprobe
from bittensor import wallets

with tempfile.TemporaryDirectory(dir='.') as tmp:
    root = Path(tmp).resolve()
    args = argparse.Namespace(home=str(root / 'home'), mode=None, wallet_name='reax', hotkey_name='miner1')
    (root / 'home/venv/bin').mkdir(parents=True)
    (root / 'home/venv/bin/python').symlink_to(sys.executable)
    with patch('pathlib.Path.home', return_value=root):
        first = hotkey(args)
        assert first.status == 'ok', first.summary
        second = hotkey(args)
        assert second.data['ss58'] == first.data['ss58']
        wallet_path = root / '.bittensor/wallets/reax'
        assert not (wallet_path / 'coldkey').exists()
        assert not (wallet_path / 'coldkeypub.txt').exists()
        assert (wallet_path / 'hotkeys/miner1').stat().st_mode & 0o777 == 0o600
        args.ss58 = ss58.encode(bytes(range(32)))
        assert coldkeypub(args).status == 'ok'
        written = json.loads((wallet_path / 'coldkeypub.txt').read_text())
        wallets.regen_coldkey_pub(ss58=args.ss58, public_key_hex=ss58.public_key_hex(args.ss58), name='reference', path=str(root))
        expected = json.loads((root / 'reference/coldkeypub.txt').read_text())
        assert written == expected
        loaded = wallets.open_wallet(name='reax', path=str(wallet_path.parent)).coldkeypub.ss58_address
        assert loaded == args.ss58
        print('PASS: real hotkey created, idempotent, mode 0600, no coldkey, exact btcli 11.1 public JSON, native readback')
    # Force the native 11.1 fallback even if the optional legacy wallet module was installed.
    prefix = '''import builtins
_original_import = builtins.__import__
def _import(name, *args, **kwargs):
    if name == 'bittensor_wallet':
        raise ImportError('integration: force native fallback')
    return _original_import(name, *args, **kwargs)
builtins.__import__ = _import
'''
    output = sysprobe.run([sys.executable, '-c', prefix + BACKEND_SCRIPT, 'native', 'miner1', str(root / 'native-wallets')], timeout=30)
    value = json.loads(output['stdout'])
    assert 'ss58' in value, value.get('error')
    ss58.decode(value['ss58'])
    assert not (root / 'native-wallets/native/coldkey').exists()
    print('PASS: native bittensor 11.1 fallback creates hotkey only')
