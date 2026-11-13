"""Additional inert document formats with bounded text and no URL fetching."""
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
import zlib

MAX_TEXT = 200000
TEXT = '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}'
OFFICE = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
TABLE = '{urn:oasis:names:tc:opendocument:xmlns:table:1.0}'


def odt_text(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries)>256 or len(names)!=len(set(names)) or sum(item.file_size for item in entries)>8*1024*1024
                or any(item.flag_bits&1 or item.compress_type not in {zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED}
                       or PurePosixPath(item.filename).is_absolute() or '..' in PurePosixPath(item.filename).parts
                       or item.filename.lower().startswith(('basic/','scripts/')) for item in entries)):
                raise ValueError('ODT превышает безопасные ограничения или содержит макросы.')
            if archive.getinfo('mimetype').file_size>100 or archive.read('mimetype')!=b'application/vnd.oasis.opendocument.text':
                raise ValueError('Это не текстовый документ ODT.')
            if archive.getinfo('content.xml').file_size>2*1024*1024:
                raise ValueError('Слишком большой XML внутри ODT.')
            xml = archive.read('content.xml')
            if b'\0' in xml or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                raise ValueError('Кодировка или объявления XML этого ODT не поддерживаются.')
            document = ET.fromstring(xml)
            body = document.find(OFFICE+'body')
            body = body.find(OFFICE+'text') if body is not None else None
            if body is None:raise ValueError('В ODT нет основного текста.')
            omitted = {TEXT+'tracked-changes', TEXT+'note', OFFICE+'annotation', OFFICE+'annotation-end'}
            selected = []
            nodes = [(body,0,False)]
            visited = 0
            while nodes:
                node,depth,in_paragraph = nodes.pop();visited += 1
                if visited>50000 or depth>64:raise ValueError('Слишком сложная структура ODT.')
                if node.tag in omitted:continue
                if node.tag in {TEXT+'p',TEXT+'h'}:
                    if in_paragraph:raise ValueError('Вложенные абзацы ODT не поддерживаются.')
                    selected.append(node)
                nodes.extend((child,depth+1,in_paragraph or node.tag in {TEXT+'p',TEXT+'h'}) for child in reversed(list(node)))
            def collect(node):
                if node.tag in omitted:return ''
                if node.tag==TEXT+'s':
                    count = int(node.get(TEXT+'c','1'))
                    if not 1<=count<=1000:raise ValueError('Слишком длинный отступ ODT.')
                    return ' '*count
                if node.tag==TEXT+'tab':return '\t'
                if node.tag==TEXT+'line-break':return '\n'
                return (node.text or '') + ''.join(collect(child)+(child.tail or '') for child in node)
            paragraphs=[];total=0
            for node in selected:
                value=collect(node);total+=len(value)+2
                if total>MAX_TEXT:raise ValueError('Извлечённый текст превышает 200 000 знаков.')
                paragraphs.append(value)
            if any(node.get(TABLE+'number-columns-repeated','1')!='1' or node.get(TABLE+'number-rows-repeated','1')!='1' for node in body.iter()):
                raise ValueError('ODT с повторяющимися строками/ячейками таблицы не поддерживается. Сохраните как DOCX.')
            return '\n\n'.join(paragraphs), ['Извлечены основной текст и таблицы ODT; сноски, комментарии и история правок не включены. Проверьте порядок абзацев.']
    except (zipfile.BadZipFile,KeyError,ET.ParseError,RuntimeError,RecursionError,OSError,EOFError,zlib.error,UnicodeError):
        raise ValueError('ODT повреждён, зашифрован или использует неподдерживаемую структуру.') from None


class InertHTML(HTMLParser):
    OMIT = {'head','script','style','noscript','template','svg','math','iframe','object'}
    BLOCK = {'p','div','article','section','header','footer','aside','main','nav','h1','h2','h3','h4','h5','h6','li','tr','table','blockquote','pre'}
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[];self.parts=[];self.total=0;self.nodes=0
    def append(self,text):
        self.total+=len(text)
        if self.total>MAX_TEXT:raise ValueError('Извлечённый текст превышает 200 000 знаков.')
        self.parts.append(text)
    def handle_starttag(self,tag,attrs):
        self.nodes+=1
        if self.nodes>50000 or len(self.stack)>128:raise ValueError('Слишком сложная структура HTML.')
        attributes=dict(attrs)
        hidden=(any(item[1] for item in self.stack) or tag in self.OMIT or 'hidden' in attributes
                or attributes.get('aria-hidden','').lower()=='true'
                or bool(re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)',attributes.get('style',''),re.I)))
        if not hidden:
            if tag in self.BLOCK or tag in {'br','hr'}:self.append('\n')
            elif tag in {'td','th'}:self.append('\t')
        if tag not in self.VOID:self.stack.append((tag,hidden))
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,-1,-1):
            if self.stack[index][0]==tag:
                hidden=self.stack[index][1];del self.stack[index:]
                if not hidden and tag in self.BLOCK:self.append('\n')
                return
    def handle_data(self,data):
        if not any(item[1] for item in self.stack):self.append(data)


def html_text(data):
    charset=re.search(rb'<meta\b[^>]*charset\s*=\s*["\x27]?\s*([A-Za-z0-9_-]+)',data[:8192],re.I)
    encoding=charset.group(1).decode().lower() if charset else 'utf-8-sig'
    aliases={'utf8':'utf-8','windows-1251':'cp1251','windows-1252':'cp1252'}
    encoding=aliases.get(encoding,encoding)
    if encoding not in {'utf-8','utf-8-sig','cp1251','cp1252'}:
        raise ValueError('HTML поддерживается в UTF-8 или Windows-1251/1252.')
    try:content=data.decode(encoding)
    except UnicodeError:raise ValueError('Не удалось прочитать кодировку HTML. Сохраните файл в UTF-8.') from None
    parser=InertHTML();parser.feed(content);parser.close()
    text=re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+','\n\n',''.join(parser.parts)).strip()
    return text,['HTML прочитан как текст: скрипты, стили и встроенные объекты не запускаются, ссылки не загружаются. Внешние CSS и JavaScript не учитываются; проверьте результат.']


def isolated_text(data,mode):
    if mode=='doc' and not data.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        raise ValueError('Ожидается старый двоичный DOC. Для RTF, HTML и DOCX используйте соответствующее расширение.')
    with tempfile.TemporaryDirectory(prefix='istok-document-') as directory, tempfile.TemporaryFile() as output:
        try:
            result=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).with_name('document_worker.py')),mode],
                input=data,stdout=output,stderr=subprocess.DEVNULL,cwd=directory,
                env={'PATH':os.defpath,'PYTHONIOENCODING':'utf-8'},timeout=6,close_fds=True,check=False)
            output.seek(0);raw=output.read(1300001)
            if len(raw)>1300000:raise ValueError('Документ превысил ограничения обработки.')
            if mode=='doc':
                if result.returncode:raise ValueError('DOC не прочитан: файл повреждён, зашифрован или формат не поддерживается. Сохраните его как DOCX.')
                text=raw.decode('utf-8-sig')
            else:
                payload=json.loads(raw)
                if payload.get('error'):raise ValueError(payload['error'])
                if result.returncode:raise ValueError('RTF не удалось прочитать.')
                text=payload['text']
            if not isinstance(text,str) or len(text)>MAX_TEXT:raise ValueError('Извлечённый текст превышает 200 000 знаков.')
            return text,[('Извлечён текст старого DOC. Сложные элементы и порядок таблиц могут отличаться; проверьте результат.' if mode=='doc' else 'RTF преобразован в текст, без выполнения полей и встроенных объектов; колонтитулы, сноски и комментарии не включены. Проверьте результат.')]
        except subprocess.TimeoutExpired:raise ValueError('Время обработки документа истекло. Сохраните файл как DOCX или TXT.') from None
        except (OSError,UnicodeError,KeyError,json.JSONDecodeError):raise ValueError('Не удалось безопасно прочитать документ.') from None
