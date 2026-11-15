import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from cloud import modal_web
from plagiarism.access import AccessError
from plagiarism.search_pool import fingerprint
from plagiarism.search_key_vault import KeyVault
from tests.test_search_pool import KEY, grant
from tests.test_search_serpapi import account


def packet():
    now=time.time()
    keys={p:KEY for p in ('serpapi','serper','exa')};keys['tavily']='tvly-'+KEY
    observed=account();observed.pop('api_key');observed.pop('account_email')
    usage={'account':{'current_plan':'Researcher','plan_limit':1000,'plan_usage':809,
                      'paygo_usage':0,'paygo_limit':None},'key':{'usage':809,'limit':None}}
    return keys,{'version':1,'packet_id':'a'*32,'captured_at':now,
        'credential_fingerprints':{p:fingerprint(k) for p,k in keys.items()},
        'proofs':{p:{'key_fingerprint':fingerprint(keys[p]),'checked_at':now,
                    'usage':observed if p=='serpapi' else usage} for p in ('serpapi','tavily')}}


class IntegratedDeployTests(unittest.TestCase):
    def test_production_mount_symlink_is_resolved_before_server_and_vault(self):
        keys,proof=packet()
        with TemporaryDirectory() as directory:
            target=Path(directory)/'volume';target.mkdir()
            mount=Path(directory)/'state';mount.symlink_to(target,target_is_directory=True)
            for integrated in (False,True):
                environment={'ISTOK_INTEGRATED_RELEASE':'1' if integrated else '0',
                    'ISTOK_REGISTRATION_ENABLED':'0','ISTOK_DASHBOARD_WRITES_ENABLED':'0',
                    'ISTOK_AUTOMATIC_MONTHLY_LIMIT':'900','ISTOK_POOL_BOOTSTRAP':json.dumps(proof),
                    'ISTOK_SEARCH_POOL_GRANTS':json.dumps({'serper':grant(),'exa':grant('exa')})}
                sdk=MagicMock()
                with self.subTest(integrated=integrated),patch.dict('sys.modules',{'modal':sdk}), \
                     patch.dict('os.environ',environment,clear=True),patch('pathlib.Path',return_value=mount), \
                     patch('cloud.web_beta.create_server') as create,patch('threading.Thread'), \
                     patch('plagiarism.search_credentials.key_for',side_effect=keys.get):
                    modal_web.serve_beta()
                root=create.call_args.args[0]
                self.assertEqual(root,target.resolve(strict=True))
                self.assertFalse(root.is_symlink())
                self.assertEqual(create.call_args.kwargs.get('integrated',False),integrated)
                self.assertEqual(KeyVault(root,lambda:None).root,target.resolve(strict=True)/'search-pool-vault')

    def test_missing_production_mount_fails_before_server_start(self):
        with TemporaryDirectory() as directory,patch.dict('sys.modules',{'modal':MagicMock()}), \
             patch.dict('os.environ',{'ISTOK_INTEGRATED_RELEASE':'0'},clear=True), \
             patch('pathlib.Path',return_value=Path(directory)/'missing'), \
             patch('cloud.web_beta.create_server') as create,patch('threading.Thread') as thread:
            with self.assertRaises(FileNotFoundError):modal_web.serve_beta()
        create.assert_not_called();thread.assert_not_called()

    def test_arbitrary_vault_root_symlink_remains_rejected(self):
        with TemporaryDirectory() as directory:
            target=Path(directory)/'volume';target.mkdir()
            mount=Path(directory)/'untrusted';mount.symlink_to(target,target_is_directory=True)
            with self.assertRaises(AccessError) as caught:KeyVault(mount,lambda:None)
            self.assertEqual(caught.exception.code,'pool_storage_unavailable')
            self.assertFalse((target/'search-pool-vault').exists())

    def test_same_source_reader_writer_only_secret_role_flags_differ(self):
        keys,proof=packet();sdk=MagicMock()
        config={'username':'dashboardtest','salt':'a'*64,'digest':'b'*64}
        with TemporaryDirectory() as directory,patch.dict('sys.modules',{'modal':sdk}),patch.object(modal_web,'ROOT',Path(directory)):
            for writer in (False,True):
                modal_web.build_app(directory,keys['tavily'],google_key=keys['serpapi'],serper_key=keys['serper'],exa_key=keys['exa'],
                    automatic_search=True,integrated=True,registration_enabled=writer,business_writes_enabled=writer,
                    automatic_monthly_limit=900,dashboard_config=config,pool_bootstrap=proof,
                    pool_grants={'serper':grant(),'exa':grant('exa')})
                secret=sdk.Secret.from_dict.call_args.args[0]
                self.assertEqual(secret['ISTOK_REGISTRATION_ENABLED'],'1' if writer else '0')
                self.assertEqual(secret['ISTOK_DASHBOARD_WRITES_ENABLED'],'1' if writer else '0')
                self.assertEqual(secret['ISTOK_AUTOMATIC_MONTHLY_LIMIT'],'900')
                self.assertEqual(json.loads(secret['ISTOK_POOL_BOOTSTRAP']),proof)
                image_env=sdk.Image.debian_slim.return_value.apt_install.return_value.pip_install.return_value.add_local_dir.return_value.env.call_args.args[0]
                for value in (*keys.values(),*config.values()):self.assertNotIn(value,json.dumps(image_env))
                self.assertNotIn('gpu',sdk.App.return_value.function.call_args.kwargs)

    def test_invalid_or_stale_packet_rejected_before_sdk(self):
        keys,proof=packet();sdk=MagicMock();config={'username':'dashboardtest','salt':'a'*64,'digest':'b'*64}
        with TemporaryDirectory() as directory,patch.dict('sys.modules',{'modal':sdk}),patch.object(modal_web,'ROOT',Path(directory)):
            for changes in ({'automatic_monthly_limit':True},{'registration_enabled':1},
                            {'pool_bootstrap':{**proof,'captured_at':time.time()-301}},
                            {'dashboard_config':{**config,'password':'never-accepted'}}):
                opts=dict(automatic_search=True,integrated=True,automatic_monthly_limit=900,
                          dashboard_config=config,pool_bootstrap=proof,
                          pool_grants={'serper':grant(),'exa':grant('exa')})
                opts.update(changes)
                with self.assertRaises((ValueError,AccessError)):
                    modal_web.build_app(directory,keys['tavily'],google_key=keys['serpapi'],serper_key=keys['serper'],exa_key=keys['exa'],**opts)
            sdk.Volume.from_name.assert_not_called();sdk.Secret.from_dict.assert_not_called()

    def test_unknown_runtime_role_never_silently_starts_legacy_server(self):
        sdk=MagicMock()
        cases=({'ISTOK_INTEGRATED_RELEASE':'unknown'},
               {'ISTOK_INTEGRATED_RELEASE':'1','ISTOK_REGISTRATION_ENABLED':'bad'},
               {'ISTOK_INTEGRATED_RELEASE':'1','ISTOK_REGISTRATION_ENABLED':'0',
                'ISTOK_DASHBOARD_WRITES_ENABLED':'bad'},
               {'ISTOK_INTEGRATED_RELEASE':'1','ISTOK_REGISTRATION_ENABLED':'0',
                'ISTOK_DASHBOARD_WRITES_ENABLED':'0','ISTOK_AUTOMATIC_MONTHLY_LIMIT':'900.0'})
        for environment in cases:
            with self.subTest(environment=environment),patch.dict('sys.modules',{'modal':sdk}),patch.dict('os.environ',environment,clear=True):
                with self.assertRaises(ValueError):modal_web.serve_beta()
        sdk.Volume.from_name.assert_not_called()

    def test_manifest_includes_all_integrated_modules_and_no_private_state(self):
        names={r['path'] for r in modal_web.source_manifest()}
        self.assertTrue({'cloud/pool_runtime.py','cloud/dashboard.py','cloud/user_auth.py',
                         'cloud/report_reuse_runtime.py','plagiarism/report_reuse.py',
                         'web/dashboard.html','web/pool.js','web/account.html'}<=names)
        self.assertFalse(any('.runtime' in p or '.sqlite' in p or 'credentials' in p and not p.endswith('search_credentials.py') for p in names))

    def test_staged_only_integrated_runtime_constructs_without_repo_fallback_or_sdk(self):
        root=Path(__file__).resolve().parents[1]
        manifest=modal_web.source_manifest(root)
        canaries=('cloud/not_uploaded.py','.runtime/private.json','web/not_uploaded.html')
        with TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.mkdir()
            for item in manifest:
                target=source/item['path'];target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(root/item['path'],target)
            for name in canaries:
                target=source/name;target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text('SYNTHETIC_NOT_FOR_UPLOAD')
            stage,actual=modal_web.stage_source(source)
            self.assertEqual(actual,manifest)
            self.assertEqual({str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file()},
                             {item['path'] for item in manifest})
            for name in canaries:self.assertFalse((stage/name).exists())
            child=r'''
import importlib.abc, importlib.machinery, json, pathlib, socket, sys, tempfile
stage=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(stage))
attempts=[]
class StagedOnly(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        root=fullname.split('.')[0]
        if root=='modal':raise AssertionError('SDK import forbidden')
        if root not in {'cloud','plagiarism','server'}:return None
        search=[str(stage)] if path is None else list(path)
        assert all(pathlib.Path(p).resolve().is_relative_to(stage) for p in search)
        spec=importlib.machinery.PathFinder.find_spec(fullname,search)
        if spec is None:raise ModuleNotFoundError('No staged module named '+fullname,name=fullname)
        if spec.origin:assert pathlib.Path(spec.origin).resolve().is_relative_to(stage)
        assert all(pathlib.Path(p).resolve().is_relative_to(stage) for p in spec.submodule_search_locations or [])
        return spec
sys.meta_path.insert(0,StagedOnly())
def forbidden(*args,**kwargs):
    attempts.append(True);raise AssertionError('Outbound I/O forbidden')
socket.socket.connect=socket.socket.connect_ex=socket.getaddrinfo=forbidden
# Only the owned loopback bind's display name is deterministic; binding is real.
def loopback_name(host):
    if host!='127.0.0.1':return forbidden()
    return 'localhost'
socket.getfqdn=loopback_name
from cloud.web_beta import create_server
roles=[]
with tempfile.TemporaryDirectory() as temporary:
    for writer in (False,True):
        service=create_server(pathlib.Path(temporary)/str(writer),'http://127.0.0.1:18541',
            host='127.0.0.1',port=0,allow_http=True,integrated=True,
            registration_enabled=writer,business_writes_enabled=writer)
        try:
            import cloud.report_reuse_runtime as runtime
            import plagiarism.report_reuse as helper
            assert isinstance(service.beta.tenants.report_reuse,runtime.ReportReuseRuntime)
            assert pathlib.Path(runtime.__file__).resolve().is_relative_to(stage)
            assert pathlib.Path(helper.__file__).resolve().is_relative_to(stage)
            publisher=service.beta.tenants.report_reuse.worker
            assert publisher.is_alive() and service.socket.fileno()>=0
            roles.append({'writer':writer,'runtime_origin':runtime.__file__,'helper_origin':helper.__file__})
        finally:
            service.server_close();service.beta.tenants.close()
        assert not publisher.is_alive() and service.socket.fileno()==-1
assert not attempts and 'modal' not in sys.modules
print(json.dumps({'roles':roles,'external_attempts':len(attempts),'SDK_imported':False,'teardown':True}))
'''
            result=subprocess.run([sys.executable,'-I','-B','-c',child,str(stage)],cwd=stage,
                env={'PATH':os.defpath,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            proof=json.loads(result.stdout)
            self.assertEqual([row['writer'] for row in proof['roles']],[False,True])
            self.assertEqual(proof['external_attempts'],0)
            self.assertFalse(proof['SDK_imported']);self.assertTrue(proof['teardown'])
