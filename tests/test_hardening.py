import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import patch
from reaxlib import descriptor, state, cmd_install, cmd_runtime, cmd_service, localnet_ops, cmd_wallet
from release_fixtures import live_private_release, registered_pending_release

BASE = registered_pending_release()
# the controller itself runs on 3.8; install's tool-Python gate is not what these tests exercise
INSTALLABLE = copy.deepcopy(BASE)
INSTALLABLE['requirements']['python_min'] = '3.8'

class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.args = SimpleNamespace(home=str(self.home), offline=False, mode='localnet', pool='s1-fast', confirm_mainnet=False)
        descriptor.begin(self.args)

    def test_descriptor_rejects_review_payloads(self):
        changes = [lambda d: d['images'].update(miner='evil.example/x/${HF_TOKEN}'),
                   lambda d: (d['status'].update(live=True), d['images'].update(public=True)),
                   lambda d: d['source'].update(ref='main'),
                   lambda d: d['source'].update(git='--upload-pack=x'),
                   lambda d: d['requirements'].update(never_publish_ports=[]),
                   lambda d: d['update'].update(auto_safe='no'),
                   lambda d: d.update(tools={'--index-url=x': '1'}),
                   lambda d: d['pools'][0].update(sidecar_port='22'),
                   lambda d: d['pools'][0]['weights'].update(repo='../../x'),
                   lambda d: d['pools'][0]['weights'].update(revision='main')]
        for mutate in changes:
            d = copy.deepcopy(BASE); mutate(d)
            with self.assertRaises(ValueError): descriptor.validate(d)
        self.assertIs(descriptor.validate(copy.deepcopy(BASE))['status']['live'], False)

    def test_install_manifest_gate_before_children(self):
        state.save_config(self.args, {'manifest_id':'OLDMANIFEST'})
        with patch.object(cmd_install, 'descriptor', return_value=INSTALLABLE), patch.object(cmd_install, 'runner') as run:
            self.assertEqual(cmd_install.install(self.args).code(), 11)
            run.assert_not_called()
        self.assertEqual(state.load_config(self.args)['manifest_id'], 'OLDMANIFEST')

    def test_install_manifest_explicit_acceptance(self):
        state.save_config(self.args, {'manifest_id':'OLDMANIFEST'})
        source = self.home/'source'
        (source/'scripts/localnet').mkdir(parents=True)
        (source/'scripts/localnet/start.sh').touch()
        self.args.accept_manifest_change = True
        with patch.object(cmd_install, 'descriptor', return_value=INSTALLABLE), patch.object(cmd_install, 'prepare_tools', return_value=None), patch.dict(os.environ, REAX_SOURCE_DIR=str(source)):
            self.assertTrue(cmd_install.install(self.args).ok)
        self.assertEqual(state.load_config(self.args)['manifest_id'], BASE['pools'][0]['manifest_id'])
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)

    def test_redaction_and_truncation(self):
        for text in ['{"secretSeed": "0xdeadbeef"}', '{"secretPhrase": "abandon abandon about"}',
                     'mnemonic: abandon abandon about', 'Authorization: Bearer abc', 'api_key=sk-1', 'privateKey: 0xabc', '{"token": "a b"}']:
            redacted = cmd_runtime.safe_logs(self.home, text)
            self.assertIn('[redacted]', redacted)
            for secret in ('deadbeef', 'abandon', 'Bearer abc', 'sk-1', '0xabc', 'a b'):
                self.assertNotIn(secret, redacted)
        self.assertEqual(len(cmd_runtime.safe_logs(self.home, 'x'*999)),400)
        self.assertEqual(cmd_runtime.sanitize(self.home, {'secretSeed':'test'})['secretSeed'], '[redacted]')

    def test_invalid_names_are_usage_errors(self):
        import contextlib
        from reaxlib.cli import main
        for command in (['wallet','status'], ['wallet','hotkey'], ['register']):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(command + ['--wallet-name','../escape','--home',str(self.home),'--offline','--json']),2)
            self.assertIn('names must contain', json.loads(output.getvalue())['summary'])

    def test_child_environment_is_minimal(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'secret', 'HF_TOKEN':'secret', 'PYTHONPATH':'hostile', 'PIP_INDEX_URL':'hostile', 'REAX_TEST':'ok'}):
            env = state.child_env()
        for key in ('OPENAI_API_KEY','HF_TOKEN','PYTHONPATH','PIP_INDEX_URL'): self.assertNotIn(key,env)
        self.assertEqual(env['REAX_TEST'],'ok')

    def test_isolated_wallet_ignores_planted_json(self):
        plant = self.home / 'plant'; plant.mkdir()
        marker = self.home / 'marker'
        (plant/'json.py').write_text('open(%r,"w").write("executed")' % str(marker))
        result = subprocess.run([sys.executable,'-I','-c',cmd_wallet.BACKEND_SCRIPT,'w','h',str(self.home/'wallet')], cwd=str(plant), env=state.child_env(), capture_output=True, text=True)
        self.assertFalse(marker.exists())
        self.assertIn('os.umask(0o077)', cmd_wallet.BACKEND_SCRIPT)
        self.assertNotIn('secretPhrase',result.stdout)
        if 'ss58' in json.loads(result.stdout):
            self.assertEqual((self.home/'wallet/w/hotkeys/h').stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.home/'wallet/w/hotkeys').stat().st_mode & 0o777, 0o700)

    def test_compose_security_wallet_override_dollar_and_port(self):
        config={'pool':'s1-fast','network':'finney','netuid':1}
        with patch.dict(os.environ, REAX_WALLET_DIR=str(self.home/'$wallet')):
            cmd_install.render(self.home,BASE,config)
        text=(self.home/'compose.yml').read_text()
        for word in ('$$wallet','cap_drop: [ALL]','no-new-privileges:true','max-size: "10m"','max-file: "3"'):
            self.assertIn(word,text)
        for ip in ('127.0.0.1', 'private.example', '192.168.1.1', '::1'):
            with self.assertRaises(ValueError): cmd_install.render(self.home,BASE,dict(config,external_ip=ip))
        for port in (8101,8102,8000,'8091',True,1023,65536):
            with self.assertRaises(ValueError): cmd_install.render(self.home,BASE,dict(config,miner_port=port))

    def test_atomic_refuses_symlink_and_private_dirs(self):
        victim=self.home/'victim'; victim.write_text('keep')
        link=self.home/'compose.yml'; link.symlink_to(victim)
        with self.assertRaises(ValueError): state.write_text(link,'bad',0o644)
        self.assertEqual(victim.read_text(),'keep')
        state.private_dir(self.home/'secrets')
        self.assertEqual((self.home/'secrets').stat().st_mode&0o777,0o700)

    def test_systemd_unit_verify_and_nonroot(self):
        executable=Path(__file__).resolve().parents[1]/'reaxctl'
        with patch.dict(os.environ, SUDO_UID='1234'):
            unit=cmd_service.render_template('reax-miner.service', self.home/'$home', executable, user=False)
        self.assertIn('User=1234',unit)
        self.assertIn('$$home',unit)
        self.assertNotIn('WorkingDirectory="',unit)
        path=self.home/'test.service'; path.write_text(unit)
        import shutil
        if shutil.which('systemd-analyze'):
            result=subprocess.run(['systemd-analyze','verify',str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
        with patch.dict(os.environ,SUDO_UID='0'):
            with self.assertRaises(ValueError): cmd_service.render_template('reax-miner.service',self.home,executable,user=False)

    def test_birth_guard_and_comm_spaces(self):
        with patch.object(localnet_ops.pathlib.Path,'read_text', return_value='123 (name with ) spaces) S '+' '.join(str(i) for i in range(1,25))):
            self.assertEqual(localnet_ops.birth(123),'19')
        with patch.object(localnet_ops.os,'kill') as kill:
            self.assertFalse(localnet_ops.alive({'pid':123}))
            kill.assert_not_called()

    def test_fetch_localhost_cache_offline_invalid_and_oversize(self):
        value=live_private_release(); value['release_id']='1.2.0rc3'
        payload=[json.dumps(value).encode()]; requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(payload[0])
            def log_message(self,*args): pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with patch.dict(os.environ,{'REAX_RELEASES_URL':'http://127.0.0.1:%s/current.json'%server.server_port,'REAX_OFFLINE':'0'}):
                descriptor.begin(self.args)
                self.assertEqual(descriptor.load(self.args,fetch=True)['release_id'],'1.2.0rc3')
                self.assertEqual(descriptor.current_source(),'fetched')
                descriptor.begin(self.args); descriptor.load(self.args,fetch=True)
                self.assertEqual(descriptor.current_source(),'cache'); self.assertEqual(len(requests),1)
                state.write_json(self.home/'releases/fetched_at',{'at':0}); payload[0]=b'x'*(256*1024+1)
                descriptor.begin(self.args)
                self.assertEqual(descriptor.load(self.args,fetch=True)['release_id'],'1.2.0rc3')
                self.assertTrue(descriptor.warnings())
                self.args.offline=True; descriptor.begin(self.args); descriptor.load(self.args,fetch=True)
                self.assertEqual(len(requests),2)
        finally: server.shutdown(); server.server_close(); thread.join()

    def test_fetch_url_and_redirect_guard(self):
        for address in ('file:///tmp/a','ftp://localhost/a','http://example.com/a','https://user:pass@example.com/a'):
            with self.assertRaises(ValueError): descriptor.safe_url(address)
        import urllib.request
        req=urllib.request.Request('https://example.com/a')
        with self.assertRaises(ValueError): descriptor.SameHostRedirect().redirect_request(req,None,302,'',{},'https://evil.example/a')

    def test_source_origin_commit_and_fetch(self):
        d=copy.deepcopy(BASE); d['source'].update(public=True,commit='a'*40)
        (self.home/'source/.git').mkdir(parents=True)
        calls=[]
        def run(cmd,**kwargs):
            calls.append(cmd)
            if '--get' in cmd: return 0,d['source']['git'],' '
            if 'rev-parse' in cmd: return 0,'b'*40,''
            return 0,'',''
        with patch.dict(os.environ,{'REAX_SOURCE_DIR':''}), patch.object(cmd_install,'runner',side_effect=run):
            source,result=cmd_install.prepare_source(self.home,d)
        self.assertIsNone(source); self.assertEqual(result.code(),10)
        self.assertTrue(any('fetch' in c and '--tags' in c for c in calls))

    def test_extract_single_blocks_escape(self):
        script=self.home/'fetch.py'; root=self.home/'rootfs'
        script.write_text("import tarfile,io,sys\nb=io.BytesIO()\nwith tarfile.open(fileobj=b,mode='w') as t:\n m=tarfile.TarInfo('l'); m.type=tarfile.SYMTYPE; m.linkname='..'; t.addfile(m)\n m=tarfile.TarInfo('l/escaped'); m.size=1; t.addfile(m,io.BytesIO(b'x'))\nb.seek(0)\nwith tarfile.open(fileobj=b) as t:\n for m in t: t.extract(m,sys.argv[1])\n")
        rc,_,_=cmd_install.runner([sys.executable,'-I','-c',localnet_ops.FETCH,str(script),str(root)])
        self.assertNotEqual(rc,0); self.assertFalse((self.home/'escaped').exists())
        script.write_text(script.read_text().replace('t.extract(m,sys.argv[1])', 't.extractfile(m)'))
        rc,_,_=cmd_install.runner([sys.executable,'-I','-c',localnet_ops.FETCH,str(script),str(root)])
        self.assertNotEqual(rc,0)
