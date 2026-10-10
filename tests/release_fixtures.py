"""Explicit release states for tests that exercise launch gates."""
import copy
import json

from reaxlib import descriptor


def registered_pending_release():
    """Registered Finney status with all real-network/source gates still closed."""
    value = json.loads(descriptor.BUNDLED.read_text())
    value['status'].update(
        launch_status='registered_starting_soon',
        live=False,
        netuid=92,
        network='finney',
        testnet_netuid=None,
        notes='Chain status and release availability are reported separately.',
    )
    value['images']['public'] = False
    value['source']['public'] = False
    return value


def live_private_release():
    """Started-chain status while source/images remain private."""
    value = registered_pending_release()
    value['status'].update(launch_status='live', live=True)
    return value


def write_release(home, value):
    """Write a descriptor cache for an isolated CLI-process test."""
    import pathlib

    path = pathlib.Path(home) / 'releases' / 'current.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(copy.deepcopy(value)))
    return path
