"""Disposable native localnet orchestration. Private source stays in its checkout."""
import json
import os
import pathlib
import signal
import socket
import time
import urllib.request
from . import cmd_install as io
from .result import Result

# Own bootstrap orchestration uses ephemeral public development identities, never user coldkeys.
FETCH = r'''
import os, runpy, sys, tarfile
original = tarfile.TarFile.extractall
original_one = tarfile.TarFile.extract
original_file = tarfile.TarFile.extractfile
def contained(member, dest):
    dest = os.path.realpath(dest)
    if member.issym() and member.linkname.startswith('/'):
        member = member.replace(linkname=os.path.relpath(os.path.join(dest, member.linkname.lstrip('/')), os.path.dirname(os.path.join(dest, member.name))), deep=False)
    if member.issym() or member.islnk():
        origin = os.path.dirname(os.path.join(dest, member.name)) if member.issym() else dest
        target = os.path.realpath(os.path.join(origin, member.linkname))
        if os.path.commonpath([target, dest]) != dest:
            raise ValueError('Image link escapes destination')
        member = tarfile.tar_filter(member, dest)
        return member.replace(uid=None, gid=None, uname=None, gname=None, deep=False)
    return tarfile.data_filter(member, dest)
def extract(self, path='.', members=None, *, numeric_owner=False, filter=None):
    return original(self, path, members, numeric_owner=numeric_owner, filter=contained)
def extract_one(self, member, path='.', set_attrs=True, *, numeric_owner=False, filter=None):
    return original_one(self, member, path, set_attrs, numeric_owner=numeric_owner, filter=contained)
tarfile.TarFile.extractall = extract
tarfile.TarFile.extract = extract_one
def extract_file(self, member):
    item = self.getmember(member) if isinstance(member, str) else member
    contained(item, dest)
    return original_file(self, member)
tarfile.TarFile.extractfile = extract_file
script, dest = sys.argv[1:]
sys.argv = [script, dest]
runpy.run_path(script, run_name='__main__')
'''

BOOTSTRAP = r'''
import json, pathlib, sys, time
import bittensor as bt
from bittensor import calls
from bittensor.keyfiles import Keypair
work, endpoint = pathlib.Path(sys.argv[1]), sys.argv[2]
if not endpoint.startswith('ws://127.0.0.1:'):
    raise ValueError('dev funding requires loopback')
class Dev:
    def __init__(self, name):
        self.coldkey = self.coldkeypub = self.hotkey = Keypair.create_from_uri('//'+name)
def submit(make):
    for attempt in range(5):
        result = make()
        if result.success: return result
        if not any(x in result.message for x in ('BadProof', 'AncientBirthBlock', 'Priority is too low')):
            raise RuntimeError(result.message)
        time.sleep(2)
    raise RuntimeError(result.message)
st = bt.Subtensor(network=endpoint)
alice, owner = Dev('Alice'), Dev('ReaxOwner')
submit(lambda: st.execute(bt.Transfer(owner.hotkey.ss58_address, 5000), alice))
before = {s.netuid for s in st.subnets.all()}
submit(lambda: st.execute(bt.RegisterSubnet(owner.hotkey.ss58_address), owner))
netuid = max({s.netuid for s in st.subnets.all()}-before)
submit(lambda: st.execute(bt.StartCall(netuid), owner))
roles = {}
for name in ('miner', 'validator'):
    dev = Dev('Reax'+name.title())
    w = bt.Wallet(name=name, hotkey='default', path=str(work/'wallets'))
    w.hotkey_file.set_keypair(dev.hotkey, encrypt=False, overwrite=True)
    w._set_coldkeypub(dev.hotkey, overwrite=True)
    submit(lambda: st.execute(bt.Transfer(dev.hotkey.ss58_address, 200), alice))
    submit(lambda: st.submit_call(calls.SubtensorModule.burned_register(netuid=netuid, hotkey=dev.hotkey.ss58_address), dev))
    if name == 'validator':
        submit(lambda: st.submit_call(calls.SubtensorModule.add_stake(hotkey=dev.hotkey.ss58_address, netuid=netuid, amount_staked=100 * 10**9), dev))
    roles[name] = {'hotkey': dev.hotkey.ss58_address, 'uid': st.neurons.uid(dev.hotkey.ss58_address, netuid)}
(work/'subnet.json').write_text(json.dumps({'netuid':netuid,'endpoint':endpoint,'roles':roles}))
'''


def processes(home):
    p = home / 'localnet/processes.json'
    return json.loads(p.read_text()) if p.exists() else {}


def proc_fields(pid):
    text = pathlib.Path('/proc/{}/stat'.format(pid)).read_text()
    return text[text.rfind(')') + 2:].split()


def alive(record):
    try:
        pid = record['pid']
        if type(pid) is not int or pid <= 1 or not record.get('birth'):
            return False
        os.kill(pid, 0)
        fields = proc_fields(pid)
        return fields[19] == record['birth'] and fields[0] != 'Z'
    except (OSError, KeyError, IndexError, ValueError):
        return False


def birth(pid):
    try:
        return proc_fields(pid)[19]
    except (OSError, IndexError):
        return None


def spawn(home, name, cmd, env):
    work = home / 'localnet'
    logpath = work / (name + '.log')
    if logpath.exists():
        logpath.replace(work / (name + '.previous.log'))
    with logpath.open('wb') as log:
        proc = io.runner(cmd, env=env, spawn=True, log=log)
    if not hasattr(proc, 'pid'):
        raise RuntimeError('Could not start ' + name)
    records = processes(home)
    records[name] = {'pid': proc.pid, 'birth': birth(proc.pid)}
    io.write_json(work / 'processes.json', records)
    (work / (name + '.pid')).write_text(str(proc.pid) + '\n')
    return proc


def down(home):
    records = processes(home)
    for record in records.values():
        if alive(record):
            try:
                os.killpg(record['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(alive(r) for r in records.values()):
        time.sleep(.2)
    for record in records.values():
        if alive(record):
            try:
                os.killpg(record['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(record['pid'], os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
    work = home / 'localnet'
    if work.exists():
        io.write_json(work / 'processes.json', {})
        for p in work.glob('*.pid'):
            p.unlink()
    return Result('localnet down', summary='Owned rehearsal processes stopped.')


def health(port, host='127.0.0.1'):
    try:
        with urllib.request.urlopen('http://{}:{}/health'.format(host, port), timeout=3) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def wait_http(home, port, timeout, host='127.0.0.1'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health(port, host):
            return True
        if not all(alive(r) for r in processes(home).values()):
            return False
        time.sleep(1)
    return False


def up(args):
    home = io.home_of(args)
    config = io.read_config(home)
    if getattr(args, 'mode', None) not in (None, 'localnet') or config.get('mode', 'localnet') != 'localnet':
        return Result('localnet up', 'blocked', 'Rehearsal requires localnet mode.', exit_code=10)
    records = processes(home)
    if set(records) == {'node-one', 'node-two', 'node-three', 'miner', 'validator'} and all(alive(r) for r in records.values()):
        return started(home)
    down(home)
    d = io.descriptor(home)
    source, failure = io.prepare_source(home, d)
    if failure:
        return failure
    py = home / 'venv/bin/python'
    if not py.exists():
        return Result('localnet up', 'blocked', 'Run reaxctl install first.', exit_code=10)
    work = home / 'localnet'
    work.mkdir(parents=True, exist_ok=True)
    port = getattr(args, 'base_port', None) or config.get('base_port', 19951)
    if not 1024 <= port <= 65525:
        return Result('localnet up', 'error', 'base-port must be between 1024 and 65525.', exit_code=2)
    for n in list(range(port, port + 3)) + list(range(port + 5, port + 8)) + [port + 9]:
        try:
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(('127.0.0.1', n))
        except OSError:
            return Result('localnet up', 'blocked', 'Port {} is occupied; choose another --base-port.'.format(n), exit_code=10)
    host = getattr(args, 'host', None) or config.get('miner_host')
    if not host:
        rc, addresses, _ = io.runner(['ip', '-j', '-4', 'addr', 'show'])
        try:
            candidates = [a['local'] for interface in json.loads(addresses) for a in interface.get('addr_info', [])]
        except (ValueError, KeyError):
            candidates = []
        import ipaddress
        host = next((ip for ip in candidates if ipaddress.ip_address(ip).is_private and not ipaddress.ip_address(ip).is_loopback), None)
        if not host:
            try:
                candidate = socket.gethostbyname(socket.gethostname())
                if ipaddress.ip_address(candidate).is_private and not ipaddress.ip_address(candidate).is_loopback:
                    host = candidate
            except OSError:
                pass
    import ipaddress
    try:
        address = ipaddress.ip_address(host)
        valid_host = address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local and not address.is_unspecified
    except (ValueError, TypeError):
        valid_host = False
    if not valid_host:
        return Result('localnet up', 'blocked', 'The chain refuses loopback miner addresses; supply --host with a local private IPv4 address.', exit_code=10)
    try:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port + 9))
    except OSError:
        return Result('localnet up', 'blocked', 'The rehearsal host address is unavailable or its miner port is occupied.', exit_code=10)
    node = work / 'node'
    binary = node / 'rootfs/target/fast-runtime/release/node-subtensor'
    if not binary.exists():
        rc, out, err = io.runner([str(py), '-I', '-c', FETCH, str(source / 'scripts/localnet/fetch_node.py'), str(node)], timeout=900)
        (work / 'fetch-node.log').write_text(out + err)
        if rc:
            return Result('localnet up', 'error', 'Native subtensor download or unpack failed (GHCR access is needed once); see localnet/fetch-node.log.',
                          data={'detail': io.detail(out + err)}, exit_code=13)
    root = node / 'rootfs'
    nodecmd = [str(root / 'lib64/ld-linux-x86-64.so.2'), '--library-path',
               str(root / 'usr/lib/x86_64-linux-gnu') + ':' + str(root / 'lib/x86_64-linux-gnu'), str(binary)]
    data = work / 'chain'
    if data.exists() and not (work / 'subnet.json').exists():
        import shutil
        shutil.rmtree(str(data))  # owned failed-bootstrap data only
        if (work / 'validator-state.json').exists():
            (work / 'validator-state.json').unlink()
    data.mkdir(exist_ok=True)
    rc, spec, _ = io.runner(nodecmd + ['build-spec', '--disable-default-bootnode', '--raw', '--chain', 'local'])
    if rc:
        return Result('localnet up', 'error', 'Native node could not build its local specification.', exit_code=13)
    specpath = data / 'local.json'
    specpath.write_text(spec)
    # Runtime sources remain read-only. Installing deps supplies the real CLI's aiohttp.
    rc, _, _ = io.runner([str(py), '-I', '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple', 'aiohttp==3.14.3'], timeout=300)
    if rc:
        return Result('localnet up', 'error', 'Rehearsal dependencies could not be installed.', exit_code=13)
    env = dict(os.environ, REAX_PYTHON_SOURCE=str(source / 'src'))
    try:
        for scheme, keytype in [('Sr25519', 'aura'), ('Ed25519', 'gran')]:
            rc, _, _ = io.runner(nodecmd + ['key', 'insert', '--base-path', str(data / 'three'), '--chain', str(specpath),
                                          '--scheme', scheme, '--suri', '//Three', '--key-type', keytype])
            if rc:
                raise RuntimeError('Node dev authority preparation failed')
        for i, name in enumerate(('one', 'two', 'three')):
            role = ['--' + name] if i < 2 else ['--name', 'Three']
            spawn(home, 'node-' + name, nodecmd + ['--base-path', str(data / name)] + role + [
                '--port', str(port + 5 + i), '--rpc-port', str(port + i),
                '--chain', str(specpath), '--validator', '--rpc-cors=all', '--allow-private-ipv4',
                '--discover-local', '--unsafe-force-node-key-generation', '--rpc-max-connections', '2000', '--offchain-worker', 'never', '--no-prometheus', '--no-telemetry'], env)
        endpoint = 'ws://127.0.0.1:' + str(port)
        deadline = time.monotonic() + getattr(args, 'timeout', 120)
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request('http://127.0.0.1:' + str(port), data=b'{"jsonrpc":"2.0","id":1,"method":"chain_getHeader","params":[]}', headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if int(json.load(r)['result']['number'], 16) > 2:
                        break
            except (OSError, ValueError, KeyError, TypeError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError('Local chain readiness timed out')
        if not (work / 'subnet.json').exists():
            rc, _, err = io.runner([str(py), '-I', '-c', BOOTSTRAP, str(work), endpoint], timeout=300, env=env)
            if rc:
                (work / 'bootstrap-error.log').write_text(err)
                raise RuntimeError('Dev subnet bootstrap failed; see localnet/bootstrap-error.log')
        if (work / 'bootstrap-error.log').exists():
            (work / 'bootstrap-error.log').unlink()
        subnet = json.loads((work / 'subnet.json').read_text())
        base = dict(env, REAX_NETWORK=endpoint, REAX_NETUID=str(subnet['netuid']), REAX_WALLET_PATH=str(work / 'wallets'),
                    REAX_MECHANISM='s1', REAX_MODEL='s1-fast', REAX_ALLOW_DEV_MODELS='1',
                    REAX_S1_FAST_RUNTIME_URL='hash', REAX_WALLET_HOTKEY='default', REAX_LOG_JSON='1')
        spawn(home, 'miner', [str(py), '-I', '-c', "import os, sys; sys.path.insert(0, os.environ['REAX_PYTHON_SOURCE']); " + 'from reax_subnet.cli import miner_main; miner_main()'],
              dict(base, REAX_WALLET_NAME='miner', REAX_MINER_HOST=host, REAX_EXTERNAL_IP=host,
                   REAX_MINER_PORT=str(port + 9), REAX_MIN_VALIDATOR_STAKE='10', REAX_METAGRAPH_REFRESH_S='10'))
        if not wait_http(home, port + 9, getattr(args, 'timeout', 120), host):
            raise RuntimeError('Miner health timed out; see localnet/miner.log')
        spawn(home, 'validator', [str(py), '-I', '-c', "import os, sys; sys.path.insert(0, os.environ['REAX_PYTHON_SOURCE']); " + 'from reax_subnet.cli import validator_main; validator_main()'],
              dict(base, REAX_WALLET_NAME='validator', REAX_ALLOW_PRIVATE_IPS='1', REAX_ROUND_INTERVAL_S='10',
                   REAX_WEIGHTS_INTERVAL_BLOCKS='1', REAX_WARMUP_ROUNDS='2', REAX_EMA_ALPHA='0.3',
                   REAX_STATE_PATH=str(work / 'validator-state.json')))
        config.update(mode='localnet', network=endpoint, netuid=subnet['netuid'], base_port=port, miner_port=port + 9,
                      source_dir=str(source), miner_host=host)
        io.write_config(home, config)
        io.mark(home, 'localnet', {'endpoint': endpoint, 'netuid': subnet['netuid']})
        return started(home)
    except KeyboardInterrupt:
        down(home)
        raise
    except Exception as e:
        down(home)
        return Result('localnet up', 'error', str(e), exit_code=13)


def started(home):
    result = status(home)
    result.command = 'localnet up'
    result.summary = 'Rehearsal is running.'
    result.next = [{'command': 'reaxctl wallet status', 'why': 'Show the rehearsal public addresses.'},
                   {'command': 'reaxctl verify', 'why': 'Passes after the first validator rounds, usually within a minute.'}]
    return result


def status(home):
    records = processes(home)
    states = {name: {'pid': r['pid'], 'running': alive(r)} for name, r in records.items()}
    config = io.read_config(home)
    return Result('localnet status', summary='Rehearsal process status.', data={
        'processes': states, 'running': len(states) == 5 and all(s['running'] for s in states.values()),
        'health': health(config.get('miner_port', 19960), config.get('miner_host', '127.0.0.1')) if states else None})


def dev_operation(home, operation, address, netuid=None):
    config = io.read_config(home)
    endpoint = config.get('network', '')
    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    if config.get('mode') != 'localnet' or parsed.scheme != 'ws' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        return Result(operation, 'blocked', 'Dev operations require the owned loopback rehearsal.', exit_code=10)
    script = r'''
import sys, time
import bittensor as bt
from bittensor import calls
from bittensor.keyfiles import Keypair
class Dev:
    def __init__(self): self.coldkey=self.coldkeypub=self.hotkey=Keypair.create_from_uri('//Alice')
st=bt.Subtensor(network=sys.argv[1]); w=Dev(); address=sys.argv[3]
for attempt in range(5):
    r = st.execute(bt.Transfer(address, 200), w) if sys.argv[2]=='fund' else st.submit_call(calls.SubtensorModule.burned_register(netuid=int(sys.argv[4]),hotkey=address),w)
    if r.success: break
    if 'BadProof' not in r.message and 'AncientBirthBlock' not in r.message: raise RuntimeError(r.message)
    time.sleep(2)
else: raise RuntimeError(r.message)
'''
    rc, _, _ = io.runner([str(home / 'venv/bin/python'), '-I', '-c', script, endpoint, operation, address,
                          str(netuid or config['netuid'])], timeout=120)
    return Result(operation, 'ok' if not rc else 'error', 'Dev operation completed.' if not rc else 'Dev operation failed.', exit_code=0 if not rc else 13)


def fund(home, address, **kwargs):
    return dev_operation(pathlib.Path(home), 'fund', address)


def register_hotkey(home, address, netuid=None, **kwargs):
    return dev_operation(pathlib.Path(home), 'register', address, netuid)


def chain_evidence(home):
    """Read registration and commitment from the owned chain without signing."""
    config = io.read_config(home)
    from urllib.parse import urlparse
    endpoint = config.get('network', '')
    parsed = urlparse(endpoint)
    if config.get('mode') != 'localnet' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        return {}
    source = config.get('source_dir')
    if not source or not processes(home) or not (home / 'venv/bin/python').exists():
        return {}
    script = r'''
import json, pathlib, sys
import bittensor as bt
import os
sys.path.insert(0, os.environ['REAX_PYTHON_SOURCE'])
from reax_subnet.chain import SdkChain
work = pathlib.Path(sys.argv[1])
config = json.loads((work/'config.json').read_text())
w = bt.Wallet(name='miner',hotkey='default',path=str(work/'localnet/wallets'))
chain = SdkChain(config['network'], config['netuid'], w, commitments=True)
snapshot = chain.snapshot()
n = next((n for n in snapshot.neurons if n.hotkey==w.hotkey.ss58_address), None)
wanted = 'reax-s1/1:'+config['pool']+':'+config['manifest_id']
print(json.dumps({'registered':n is not None,'commitment_published':n is not None and n.commitment==wanted}))
'''
    rc, out, _ = io.runner([str(home / 'venv/bin/python'), '-I', '-c', script, str(home)],
                          timeout=15, env=dict(os.environ, REAX_PYTHON_SOURCE=str(pathlib.Path(source) / 'src')))
    try:
        return json.loads(out) if not rc else {}
    except ValueError:
        return {}
