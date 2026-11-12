from copy import deepcopy
import unittest
from plagiarism import html_evidence as evidence,web_fetch
from plagiarism.matching import analyze
from test_web_fetch import Fixture,Response


class EditControlTests(unittest.TestCase):
    def fetch(self,body,host='source.example'):
        fixture=Fixture();fixture.add(host,'/article',Response(body.encode()))
        return web_fetch.Fetcher(resolver=fixture.resolver,connector=fixture.connector,
            clock=lambda:fixture.now).fetch('https://'+host+'/article')

    def test_wiki_heading_and_list_match_all_ten_words(self):
        text='задачи метрологии\nСоздание общей теории измерений.\nОбразование единиц физических величин'
        page=self.fetch('<h2>Цели и задачи метрологии</h2><span class="mw-editsection">[<a href="/edit">править</a> | править код]</span><ul><li>Создание общей теории измерений.</li><li>Образование единиц физических величин и систем единиц.</li></ul>')
        result=analyze(text,[{'id':'source','url':page['url'],'title':page['title'],'text':page['text']}])
        self.assertEqual(result['matches'][0]['excerpt'],text)
        self.assertNotIn('править',page['text']);self.assertFalse(any('/edit' in l['url'] for l in page['links']))

    def test_other_mediawiki_host_and_word_boundaries(self):
        page=self.fetch('<p>Независимая статья описывает наблюдение<span class="extra mw-editsection">[edit]</span>измерение и повторную проверку результатов опыта.</p>','encyclopedia.example')
        self.assertIn('наблюдение измерение',page['text']);self.assertNotIn('edit',page['text'])

    def test_normal_wording_and_near_matching_classes_are_preserved(self):
        page=self.fetch('<p>Автор предлагает править код и обсуждает задачи метрологии.</p><span class="mw-editsection-example">Этот пример является содержимым статьи и сохраняется.</span>')
        self.assertIn('править код',page['text']);self.assertIn('пример является содержимым',page['text'])

    def test_independent_metrology_text_is_not_highlighted(self):
        page=self.fetch('<h2>Цели и задачи метрологии</h2><span class="mw-editsection">[править]</span><p>Создание общей теории измерений. Образование единиц физических величин и систем единиц.</p>')
        text='Студенты принесли в лабораторию три разных термометра. После короткого опыта они сравнили показания и обсудили причины расхождения. Для следующего занятия группа подготовила собственную таблицу наблюдений.'
        result=analyze(text,[{'id':'source','url':page['url'],'title':page['title'],'text':page['text']}])
        self.assertEqual(result['matches'],[])

    def test_historical_note_derivations_remain_valid(self):
        primary='Наблюдение за результатами опыта [1] помогает сравнить измерения двух независимых лабораторий.'
        clean=primary.replace('[1] ','')
        proof=evidence.build_derivation(primary,clean,[{'rendered_marker':'[1]'}],'a'*64)
        proof['primary_extraction']='html-parser-v2-implied-ends'
        self.assertTrue(evidence.valid_derivation(primary,clean,proof))
        fetched={'content_type':'text/html','extraction':'html-parser-v2-implied-ends','raw_html_sha256':'a'*64,
            'html_semantic_view':{'id':evidence.VIEW_ID,'policy':evidence.POLICY,'extraction':evidence.VIEW_EXTRACTION,
                'text':clean,'text_sha256':evidence.digest(clean),'raw_html_sha256':'a'*64,'derivation':proof}}
        source={'id':'old','title':'Сохранённая статья','url':'https://source.example','text':primary}
        self.assertEqual(len(evidence.source_view(source,fetched)),1)
        changed=deepcopy(fetched);changed['extraction']=evidence.PRIMARY_EXTRACTION
        self.assertEqual(evidence.source_view(source,changed),[])
        for malformed in ([],{},None):
            changed['extraction']=malformed
            self.assertEqual(evidence.source_view(source,changed),[])
