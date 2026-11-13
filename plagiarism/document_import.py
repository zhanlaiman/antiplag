"""Bounded document-to-text import. No persistence, search, OCR or URL access."""
import base64
import io
from pathlib import PurePosixPath
from threading import BoundedSemaphore
import xml.etree.ElementTree as ET
import zipfile
import zlib

from plagiarism.pdf_source import extract_pdf,PDFError
from plagiarism.document_formats import odt_text,html_text,isolated_text

MAX_BYTES=1024*1024
MAX_EXPANDED=8*1024*1024
MAX_XML=2*1024*1024
MAX_CHARACTERS=200000
SLOT=BoundedSemaphore(1)
W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def docx_text(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries=archive.infolist();names=[i.filename for i in entries]
            if (len(entries)>256 or len(names)!=len(set(names)) or sum(i.file_size for i in entries)>MAX_EXPANDED
                    or any(i.compress_type not in {zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED} or i.flag_bits&1 or i.file_size<0 or PurePosixPath(i.filename).is_absolute()
                           or '..' in PurePosixPath(i.filename).parts or i.filename.lower().endswith('vbaproject.bin') for i in entries)):
                raise ValueError('DOCX превышает безопасные ограничения или содержит неподдерживаемое содержимое.')
            if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                raise ValueError('Это не документ DOCX.')
            for name in ('[Content_Types].xml','word/document.xml'):
                if archive.getinfo(name).file_size>MAX_XML:raise ValueError('Слишком большой XML внутри DOCX.')
            types=archive.read('[Content_Types].xml');xml=archive.read('word/document.xml')
            if any(b'<!DOCTYPE' in part.upper() or b'<!ENTITY' in part.upper() for part in (types,xml)):
                raise ValueError('XML с объявлениями сущностей не поддерживается.')
            # UTF-16 XML would hide declarations in zero bytes; Word writes UTF-8.
            if b'\0' in xml or b'\0' in types:raise ValueError('Кодировка XML этого DOCX не поддерживается.')
            content_types=ET.fromstring(types)
            if not any(e.get('PartName')=='/word/document.xml' and e.get('ContentType')=='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml' for e in content_types):
                raise ValueError('Поддерживается обычный DOCX без макросов.')
            document=ET.fromstring(xml);body=document.find(W+'body')
            if body is None:raise ValueError('В DOCX нет основного текста.')
            paragraphs=[];nodes=[(body,0,False)];selected=[];visited=0
            while nodes:
                node,depth,in_paragraph=nodes.pop();visited+=1
                if visited>50000 or depth>64:raise ValueError('Слишком сложная структура DOCX.')
                if node.tag in {W+'del',W+'moveFrom'}:continue
                if node.tag==W+'p':
                    if in_paragraph:raise ValueError('Вложенные абзацы DOCX не поддерживаются.')
                    selected.append(node)
                nodes.extend((child,depth+1,in_paragraph or node.tag==W+'p') for child in reversed(list(node)))
            total=0
            def collect(element):
                if element.tag in {W+'del',W+'moveFrom'}:return ''
                if element.tag==W+'t':return element.text or ''
                if element.tag==W+'tab':return '\t'
                if element.tag in {W+'br',W+'cr'}:return '\n'
                return ''.join(collect(child) for child in element)
            for paragraph in selected:
                value=collect(paragraph);total+=len(value)+2
                if total>MAX_CHARACTERS:raise ValueError('Извлечённый текст превышает 200 000 знаков. Разделите документ.')
                paragraphs.append(value)
            text='\n\n'.join(paragraphs)
            omitted=any(n.startswith(('word/header','word/footer','word/footnotes','word/endnotes','word/comments')) for n in names)
            notes=['Извлечён основной текст DOCX и таблиц. Проверьте порядок абзацев перед поиском.']
            if omitted:notes.append('Колонтитулы, сноски и комментарии в этом импорте не включены.')
            return text,notes
    except (zipfile.BadZipFile,zipfile.LargeZipFile,KeyError,ET.ParseError,RuntimeError,NotImplementedError,RecursionError,OSError,EOFError,zlib.error,UnicodeError) as error:
        raise ValueError('DOCX повреждён или использует неподдерживаемую структуру.') from None


def extract_document(name,encoded):
    suffix=PurePosixPath(name).suffix.lower() if isinstance(name,str) else ''
    if suffix not in {'.doc','.docx','.pdf','.odt','.rtf','.html','.htm'}:raise ValueError('Импортируются DOC, DOCX, PDF, ODT, RTF и HTML. TXT/MD читаются в браузере.')
    if not isinstance(encoded,str) or len(encoded)>((MAX_BYTES+2)//3)*4:
        raise ValueError('Документ превышает 1 МиБ.')
    try:data=base64.b64decode(encoded,validate=True)
    except (ValueError,UnicodeError):raise ValueError('Повреждённые данные документа.') from None
    if not data or len(data)>MAX_BYTES:raise ValueError('Пустой документ или размер больше 1 МиБ.')
    if not SLOT.acquire(blocking=False):raise ValueError('Другой документ сейчас обрабатывается. Повторите импорт после завершения.')
    try:
        pages=None
        if suffix=='.docx':text,notes=docx_text(data)
        elif suffix=='.odt':text,notes=odt_text(data)
        elif suffix in {'.html','.htm'}:text,notes=html_text(data)
        elif suffix in {'.rtf','.doc'}:text,notes=isolated_text(data,suffix[1:])
        else:
            try:result=extract_pdf(data)
            except PDFError as error:raise ValueError(str(error)) from None
            text=result['text'];pages=result['page_count'];notes=['Извлечён текстовый слой PDF. OCR не выполняется; проверьте порядок строк.']
        if not text.strip():raise ValueError('В документе нет извлекаемого текста. Изображения и сканы не распознаются.')
        if len(text)>MAX_CHARACTERS:raise ValueError('Извлечённый текст превышает 200 000 знаков. Разделите документ.')
        if any(ord(c)<32 and c not in '\n\r\t' for c in text):raise ValueError('Документ содержит неподдерживаемые управляющие символы.')
        return {'text':text,'format':suffix[1:].upper(),'characters':len(text),'pages':pages,'notes':notes}
    finally:SLOT.release()
