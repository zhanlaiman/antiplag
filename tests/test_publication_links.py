import unittest
from plagiarism.publication_links import publication_file


class PublicationLinkTests(unittest.TestCase):
    def test_pipeline_follows_bounded_viewer_then_download_and_keeps_partial_evidence(self):
        from plagiarism.web_search import run
        from plagiarism.query_sentences import SHORT_CENTER_STRATEGY
        text='Сосна берёза клён кедр липа пихта дуб вяз ель бук ясень осина рябина ольха калина бузина черёмуха можжевельник яблоня груша'
        base='https://journal.example/jour/article/'
        class Search:
            def search(self,q,rid):return [{'url':base+'view/17'}]
        class Fetch:
            def __init__(self):self.calls=[]
            def fetch(self,url):
                self.calls.append(url)
                if url==base+'view/17':
                    return {'url':url,'text':'Сосна берёза клён кедр липа пихта — карточка статьи',
                            'links':[{'url':base+'view/17/8','text':'PDF (Rus)'}]}
                if url==base+'view/17/8':
                    return {'url':url,'text':'Здесь находится ссылка на полный текст научной публикации.',
                            'links':[{'url':base+'download/17/8','text':'Скачать PDF'}]}
                return {'url':url,'text':text,'links':[]}
        fetch=Fetch();result=run(text,searcher=Search(),fetcher=fetch,query_strategy=SHORT_CENTER_STRATEGY)
        self.assertEqual(fetch.calls,[base+'view/17',base+'view/17/8',base+'download/17/8'])
        self.assertEqual(result['stats']['matched_words'],20)
        self.assertTrue(all(x['basis']=='observed_publication_file'for x in result['web']['link_discoveries']))
        self.assertLessEqual(result['web']['urls_attempted'],result['web']['max_page_attempts'])

    def test_ojs_observed_file_same_article_only(self):
        source={'url':'https://journal.example/jour/article/view/175','links':[
            {'url':'https://journal.example/jour/article/view/999/2','text':'PDF'},
            {'url':'https://other.example/jour/article/view/175/198','text':'PDF'},
            {'url':'https://journal.example/jour/article/view/175/198','text':'PDF (Rus)'}]}
        expected=source['links'][2]['url']
        self.assertEqual(publication_file(source,set()),expected)
        self.assertIsNone(publication_file(source,{expected}))
        source['url']=expected
        source['links']=[{'url':'https://journal.example/jour/article/download/175/198','text':'PDF'}]
        self.assertEqual(publication_file(source,{expected}),source['links'][0]['url'])

    def test_pdf_catalogue_follows_actual_file_link_with_same_resource(self):
        source={'url':'https://library.example/art2024/bv2802.pdf/en/info','links':[
            {'url':'https://library.example/art2024/another.pdf','text':'PDF'},
            {'url':'https://library.example/art2024/bv2802.pdf','text':'http://library.example/art2024/bv2802.pdf'}]}
        self.assertEqual(publication_file(source,set()),source['links'][1]['url'])
        source['links']=[]
        self.assertIsNone(publication_file(source,set()))

    def test_no_root_page_cross_origin_query_fragment_or_unlabelled_link(self):
        parent='https://journal.example/article/view/175'
        for url,label in [('https://journal.example/article/download/175/198?token=secret','PDF'),
                          ('https://journal.example/article/download/175/198#file','PDF'),
                          ('http://journal.example/article/download/175/198','PDF'),
                          ('https://journal.example:8080/article/download/175/198','PDF'),
                          ('https://journal.example/article/download/175/198','Unrelated label')]:
            self.assertIsNone(publication_file({'url':parent,'links':[{'url':url,'text':label}]},set()))
        self.assertIsNone(publication_file({'url':'https://journal.example','links':[{'url':parent+'/198','text':'PDF'}]},set()))
