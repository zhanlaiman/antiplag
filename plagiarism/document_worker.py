"""Isolated RTF and legacy DOC conversion; invoked with -I and bounded stdin."""
import json
import os
from pathlib import Path
import re
import shutil
import sys


def limits():
    import resource
    for kind, value in [(resource.RLIMIT_CPU, 4), (resource.RLIMIT_FSIZE, 1300000),
                        (resource.RLIMIT_NOFILE, 32), (resource.RLIMIT_CORE, 0)]:
        resource.setrlimit(kind, (value, value))
    if sys.platform != 'darwin':
        resource.setrlimit(resource.RLIMIT_AS, (256*1024*1024, 256*1024*1024))


def rtf(data):
    if not data.lstrip().startswith(b'{\\rtf1') or re.search(rb'\\bin\d', data):
        raise ValueError('Поддерживается текстовый RTF без двоичных блоков.')
    depth = 0
    for match in re.finditer(rb'\\[\\{}]|[{}]', data):
        token = match.group()
        if token == b'{':depth += 1
        elif token == b'}':depth -= 1
        if depth < 0 or depth > 128:
            raise ValueError('Повреждённая или слишком сложная структура RTF.')
    if depth:
        raise ValueError('Не закрыта группа RTF.')
    codepage = re.search(rb'\\ansicpg(\d{1,5})\b', data[:8192])
    encoding = 'cp' + codepage.group(1).decode() if codepage else 'cp1252'
    if encoding == 'cp65001':encoding = 'utf-8'
    if encoding not in {'utf-8', 'cp866', 'cp874', 'cp932', 'cp936', 'cp949', 'cp950',
                        *('cp'+str(i) for i in range(1250, 1259))}:
        raise ValueError('Кодировка RTF не поддерживается. Сохраните документ в DOCX или UTF-8.')
    # The sole imported local dependency is pinned, vendored BSD source.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from rtf_vendor import rtf_to_text
    text = rtf_to_text(data.decode(encoding), encoding=encoding, errors='strict')
    text = text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le')
    if len(text) > 200000:
        raise ValueError('Извлечённый текст превышает 200 000 знаков.')
    return text


def main():
    limits()
    mode = sys.argv[1] if len(sys.argv) == 2 else ''
    if mode == 'doc':
        # exec keeps the resource limits and PID; no orphan subprocess on timeout.
        binary = shutil.which('antiword')
        if binary:
            os.execv(binary, [binary, '-m', 'UTF-8.txt', '-w', '0', '-'])
        if sys.platform == 'darwin' and Path('/usr/bin/textutil').is_file():
            os.execv('/usr/bin/textutil', ['textutil', '-stdin', '-format', 'doc', '-convert', 'txt', '-stdout'])
        raise ValueError('Обработчик старого DOC не установлен. Сохраните документ как DOCX.')
    if mode != 'rtf':
        raise ValueError('Неизвестный формат.')
    data = sys.stdin.buffer.read(1024*1024+1)
    if len(data) > 1024*1024:
        raise ValueError('Документ превышает 1 МиБ.')
    print(json.dumps({'text': rtf(data)}, ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, UnicodeError, LookupError, OSError, MemoryError) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=True))
        sys.exit(2)
