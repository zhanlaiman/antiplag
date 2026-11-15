import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from cloud.web_beta import BetaRuntime
from plagiarism import web_search
from plagiarism.query_sentences import SHORT_PURE_STRATEGY

TEXT=' '.join('botanical'+str(i) for i in range(180))

class IntegratedQueryStrategyTests(unittest.TestCase):
    def test_same_legacy_tavily_body_keeps_original_plan_after_integration(self):
        for configured in ('serpapi-yandex','tavily'):
            with self.subTest(configured=configured),TemporaryDirectory() as directory:
                env={'ISTOK_SEARCH_PROVIDER':configured,'ISTOK_TAVILY_API_KEY':'tvly-synthetic-test-fixture',
                     'ISTOK_WEB_WIKI_FOLLOWUP':'0','ISTOK_WEB_ZERO_FALLBACK':'0'}
                with patch.dict(os.environ,env),patch('cloud.web_beta.MediaWikiSearch'):
                    runtime=BetaRuntime(Path(directory),'https://beta.example');self.addCleanup(runtime.tenants.close)
                    before=Mock(provider='tavily',last_usage={'credits':1});before.search.return_value=[]
                    after=Mock(provider='tavily',last_usage={'credits':1});after.search.return_value=[]
                    quota=Mock();quota.open.return_value=io.BytesIO(json.dumps({'account':{'current_plan':'Researcher',
                        'plan_limit':1000,'plan_usage':0,'paygo_usage':0}}).encode())
                    with patch('cloud.web_beta.build_opener',return_value=quota),patch('cloud.web_beta.TavilySearch',return_value=before):
                        original=runtime.run_web(TEXT,'a'*32,provider='tavily')
                    runtime.pool_runtime=SimpleNamespace(monthly_limit=900,searcher=Mock(return_value=after))
                    integrated=runtime.run_web(TEXT,'b'*32,provider='tavily',pool_pin={'synthetic':True})
                    self.assertEqual(original['web']['query_strategy'],integrated['web']['query_strategy'])
                    self.assertEqual([c.args[0] for c in before.search.call_args_list],
                                     [c.args[0] for c in after.search.call_args_list])
                    expected=SHORT_PURE_STRATEGY if configured=='serpapi-yandex' else web_search.DEFAULT_QUERY_STRATEGY
                    self.assertEqual(integrated['web']['query_strategy'],expected)
