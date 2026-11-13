import base64
import io
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from plagiarism.document_import import extract_document


def encode(data):return base64.b64encode(data).decode()


def odt(xml='',extra=None):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype','application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml','''<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"><office:body><office:text>'''+xml+'''</office:text></office:body></office:document-content>''')
        for name,data in (extra or {}).items():archive.writestr(name,data)
    return output.getvalue()


class AdditionalDocumentTests(unittest.TestCase):
    def test_odt_paragraphs_table_spaces_and_notes_without_external_content(self):
        data=odt('<text:h>Заголовок</text:h><text:p>Первый<text:s text:c="2"/>абзац<text:tab/>текст<text:line-break/>строка<text:note><text:note-body><text:p>Сноска</text:p></text:note-body></text:note></text:p><table:table><table:table-row><table:table-cell><text:p>Ячейка</text:p></table:table-cell></table:table-row></table:table><office:annotation><text:p>Комментарий</text:p></office:annotation><text:tracked-changes><text:p>Удалённое</text:p></text:tracked-changes>',{'external':'https://example.org/no-fetch'})
        with patch('socket.create_connection',side_effect=AssertionError('No network')):
            result=extract_document('работа.odt',encode(data))
        self.assertEqual(result['text'],'Заголовок\n\nПервый  абзац\tтекст\nстрока\n\nЯчейка')

    def test_odt_rejects_entities_path_macros_expansion_depth_and_repeats(self):
        values=[odt('<text:p>ok</text:p>',{'../outside':'no'}),
                odt('<text:p>ok</text:p>',{'Scripts/python/code.py':'no'}),
                odt('<text:p>ok</text:p>',{'bomb':'a'*(9*1024*1024)}),
                odt('<text:p><text:s text:c="200000000"/></text:p>'),
                odt('<text:p>'+'<text:span>'*80+'deep'+'</text:span>'*80+'</text:p>'),
                odt('<table:table-row table:number-rows-repeated="300000"><text:p>x</text:p></table:table-row>')]
        for value in values:
            with self.subTest(length=len(value)),self.assertRaises(ValueError):extract_document('bad.odt',encode(value))

    def test_html_is_inert_preserves_entities_blocks_and_declared_cyrillic(self):
        content='''<!doctype html><html><head><title>Не включать</title><script>fetch('https://example.org')</script></head><body><h1>Заголовок</h1><p>Первый &amp; второй.<br>Строка.</p><p hidden>Скрыто</p><div style="display:none">Тоже скрыто</div><iframe src="http://127.0.0.1">Не включать</iframe><table><tr><td>Ячейка</td><td>Текст</td></tr></table></body></html>'''
        with patch('socket.create_connection',side_effect=AssertionError('No network')):
            result=extract_document('x.html',encode(content.encode()))
        self.assertIn('Первый & второй.\nСтрока.',result['text'])
        self.assertIn('Ячейка\tТекст',result['text'])
        for fragment in ['Не включать','Скрыто','скрыто','fetch','example.org']:self.assertNotIn(fragment,result['text'])
        result=extract_document('x.htm',encode('<meta charset="windows-1251"><p>Русский текст</p>'.encode('cp1251')))
        self.assertEqual(result['text'],'Русский текст')

    def test_rtf_unicode_cyrillic_escaped_braces_tables_and_ignored_objects(self):
        rtf=br'{\rtf1\ansi\ansicpg1251 {\fonttbl{\f0 Arial;}}\f0 \u1055?\u1088?\u1080?\u1074?\u1077?\u1090?!\par \'cf\'f0\'e8\'e2\'e5\'f2.\tab cell\par \{text\} {\*\objdata 010203}{\header ignore}{\footnote omitted}}'
        result=extract_document('x.rtf',encode(rtf))
        self.assertIn('Привет!\nПривет.\tcell\n{text}',result['text'])
        self.assertNotIn('010203',result['text']);self.assertNotIn('ignore',result['text']);self.assertNotIn('omitted',result['text'])
        raw='{\\rtf1\\ansi\\ansicpg1251 Привет.}'.encode('cp1251')
        self.assertEqual(extract_document('x.rtf',encode(raw))['text'],'Привет.')

    def test_bad_legacy_files_fail_with_no_text_and_utf16_rtf_surrogates_work(self):
        for name,data in [('x.rtf',b'{\\rtf1 bad'),('x.rtf',b'{\\rtf1\\bin99 bad}'),('x.doc',b'fake DOC'),('x.doc',bytes.fromhex('d0cf11e0a1b11ae1')+b'broken')]:
            with self.subTest(name=name),self.assertRaises(ValueError):extract_document(name,encode(data))
        self.assertEqual(extract_document('x.rtf',encode(br'{\rtf1\uc1 \u-10179?\u-8704?}'))['text'],'😀')

    @unittest.skipUnless(Path('/usr/bin/textutil').is_file(),'Apple textutil creates an authored legacy fixture')
    def test_actual_authored_legacy_doc(self):
        with TemporaryDirectory() as directory:
            source=Path(directory)/'input.txt';target=Path(directory)/'input.doc'
            text='Авторский контроль старого документа. Здесь только искусственный текст для проверки импорта.'
            source.write_text(text)
            subprocess.run(['/usr/bin/textutil','-convert','doc','-output',str(target),str(source)],check=True,capture_output=True,timeout=10)
            result=extract_document('input.doc',encode(target.read_bytes()))
            self.assertEqual(result['text'].strip(),text)


if __name__=='__main__':unittest.main()
