import json
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
                         'web/dashboard.html','web/pool.js','web/account.html'}<=names)
        self.assertFalse(any('.runtime' in p or '.sqlite' in p or 'credentials' in p and not p.endswith('search_credentials.py') for p in names))
