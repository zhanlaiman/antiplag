import base64
import io
import unittest
import zipfile
from unittest.mock import patch
from plagiarism.document_import import extract_document
from tests.test_pdf_source import owned_pdf,UNICODE


def docx(xml=None,extra=None):
    xml=xml or '<w:p><w:r><w:t>Первый абзац.</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Таблица.</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+xml+'</w:body></w:document>')
        for name,data in (extra or {}).items():z.writestr(name,data)
    return stream.getvalue()


def encoded(data):return base64.b64encode(data).decode()


class DocumentImportTests(unittest.TestCase):
    def test_docx_preserves_main_paragraph_table_and_accepted_changes_without_following_links(self):
        xml='<w:p><w:r><w:t>Начало</w:t><w:tab/></w:r><w:del><w:r><w:t>Удалено</w:t></w:r></w:del><w:ins><w:r><w:t>продолжение</w:t><w:br/><w:t>строка</w:t></w:r></w:ins></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Ячейка</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        with patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
            r=extract_document('работа.DOCX',encoded(docx(xml,{'word/_rels/document.xml.rels':'https://example.org/private','word/footnotes.xml':'omitted'})))
        self.assertEqual(r['text'],'Начало\tпродолжение\nстрока\n\nЯчейка')
        self.assertTrue(any('сноски' in n for n in r['notes']));self.assertEqual(r['characters'],len(r['text']))

    def test_corrupt_archives_unsupported_files_and_base64_fail_without_text(self):
        corrupt=bytearray(docx())
        with zipfile.ZipFile(io.BytesIO(corrupt)) as archive:
            entry=archive.getinfo('word/document.xml');offset=entry.header_offset+30+len(entry.filename.encode())+len(entry.extra)
        corrupt[offset]=255
        for name,data in [('corrupt-deflate.docx',encoded(bytes(corrupt))),('broken.docx',encoded(b'PKbad')),('photo.jpg',encoded(b'image')),('bad.pdf','not base64')]:
            with self.subTest(name=name),self.assertRaises(ValueError):extract_document(name,data)

    def test_docx_rejects_expansion_macro_path_and_deep_tree(self):
        samples=[docx(extra={'large.xml':'x'*(9*1024*1024)}),docx(extra={'word/vbaProject.bin':'macro'}),
                 docx(extra={'../outside':'no'}),docx('<w:p>'+'<w:r>'*100+'<w:t>text</w:t>'+'</w:r>'*100+'</w:p>'),
                 docx('<w:p><w:p><w:r><w:t>nested</w:t></w:r></w:p></w:p>')]
        for data in samples:
            with self.subTest(bytes=len(data)),self.assertRaises(ValueError):extract_document('x.docx',encoded(data))

    def test_docx_rejects_xml_entities_and_oversized_documents(self):
        with self.assertRaises(ValueError):extract_document('x.docx',encoded(docx('<!DOCTYPE x [<!ENTITY x "boom">]><w:p><w:r><w:t>&x;</w:t></w:r></w:p>')))
        with self.assertRaises(ValueError):extract_document('x.docx',encoded(b'x'*(1024*1024+1)))
        with self.assertRaises(ValueError):extract_document('x.docx',encoded(docx('<w:p><w:r><w:t>'+'я'*200001+'</w:t></w:r></w:p>')))

    def test_real_unicode_pdf_has_text_and_scans_or_large_page_counts_fail(self):
        r=extract_document('работа.pdf',encoded(owned_pdf(UNICODE)))
        self.assertIn(UNICODE,r['text']);self.assertEqual(r['pages'],1)
        for data in [owned_pdf(image_only=True),owned_pdf(pages=41),b'%PDF-broken']:
            with self.subTest(bytes=len(data)),self.assertRaises(ValueError):extract_document('x.pdf',encoded(data))
