import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from cloud import modal_web
from tests.test_search_pool import grant, KEY


class PoolDeployTests(unittest.TestCase):
    def test_opt_in_carries_four_secrets_and_grants_without_keys_in_image_env(self):
        sdk=MagicMock()
        with TemporaryDirectory() as directory,patch.dict('sys.modules',{'modal':sdk}), \
             patch.object(modal_web,'ROOT',Path(directory)):
            modal_web.build_app(directory,KEY,google_key=KEY,serper_key=KEY,exa_key=KEY,
                default_search_provider='serpapi-yandex',automatic_search=True,
                pool_grants={'serper':grant(),'exa':grant('exa')})
        secret=sdk.Secret.from_dict.call_args.args[0]
        self.assertEqual(secret['ISTOK_AUTOMATIC_SEARCH'],'1')
        self.assertEqual({secret['ISTOK_'+p+'_API_KEY']for p in ('TAVILY','SERPAPI','SERPER','EXA')},{KEY})
        self.assertEqual(set(json.loads(secret['ISTOK_SEARCH_POOL_GRANTS'])),{'serper','exa'})
        image_env=sdk.Image.debian_slim.return_value.apt_install.return_value.pip_install.return_value.add_local_dir.return_value.env.call_args.args[0]
        self.assertNotIn(KEY,json.dumps(image_env))
        limits=sdk.App.return_value.function.call_args.kwargs
        self.assertEqual((limits['cpu'],limits['memory'],limits['min_containers'],limits['max_containers']),(0.25,512,0,1))
        self.assertNotIn('gpu',limits)

    def test_legacy_builder_does_not_enable_pool_and_invalid_free_grants_create_no_secret(self):
        sdk=MagicMock()
        with TemporaryDirectory() as directory,patch.dict('sys.modules',{'modal':sdk}), \
             patch.object(modal_web,'ROOT',Path(directory)):
            modal_web.build_app(directory,KEY)
            self.assertNotIn('ISTOK_AUTOMATIC_SEARCH',sdk.Secret.from_dict.call_args.args[0])
            sdk.Secret.reset_mock()
            for kwargs in ({'serper_key':None,'exa_key':KEY,'pool_grants':{}},
                           {'serper_key':KEY,'exa_key':KEY,'pool_grants':{'serper':{**grant(),'cash_spend_limit_usd':1},'exa':grant('exa')}}):
                with self.assertRaises((ValueError,RuntimeError)):
                    modal_web.build_app(directory,KEY,google_key=KEY,automatic_search=True,**kwargs)
            sdk.Secret.from_dict.assert_not_called()


if __name__=='__main__':unittest.main()
