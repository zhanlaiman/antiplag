"""Bounded Russian dictionary checks and explicit rules; no model or network."""
from collections import Counter
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import BoundedSemaphore

DICTIONARY = Path(__file__).resolve().parents[1] / 'data/dictionaries/ru_RU/ru_RU'
WORD = re.compile(r'[А-Яа-яЁё]+')
LINK = re.compile(r'https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.[\w-]+', re.I)
LOCK = BoundedSemaphore(1)
MAX_CHARACTERS = 10000
MAX_WORDS = 800
MAX_ISSUES = 200


def available():
    return bool(shutil.which('hunspell') and DICTIONARY.with_suffix('.aff').is_file()
                and DICTIONARY.with_suffix('.dic').is_file())


def _run(mode, words):
    """No shell, no filenames from input, no private dictionary or user env."""
    binary = shutil.which('hunspell')
    if not binary:
        raise RuntimeError('Словарный движок недоступен.')
    with tempfile.TemporaryDirectory(prefix='istok-dictionary-') as directory:
        environment = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                       'DICPATH': str(DICTIONARY.parent)}
        # ^ quotes data in the ispell pipe protocol; words contain letters only.
        content = ''.join(('^' if mode == '-a' else '') + word + '\n' for word in words)
        result = subprocess.run([binary, mode, '--no-default-personal', '-i', 'UTF-8', '-d', str(DICTIONARY), '-p', '/dev/null'],
                                input=content, text=True, encoding='utf-8', capture_output=True,
                                env=environment, cwd=directory, timeout=12, check=False)
        if result.returncode != 0 or len(result.stdout) > 2_000_000:
            raise RuntimeError('Словарная проверка временно недоступна.')
        return result.stdout


def _dictionary(words):
    output = _run('-a', words)
    blocks = output.split('\n\n')
    if blocks:
        blocks[0] = '\n'.join(blocks[0].splitlines()[1:])  # ispell banner
    blocks = [block.strip() for block in blocks if block.strip()]
    if len(blocks) != len(words):
        raise RuntimeError('Не удалось прочитать ответ словаря.')
    unknown = {}
    for word, block in zip(words, blocks):
        line = block.splitlines()[0]
        if line.startswith(('& ', '# ', '? ')):
            suggestions = line.split(': ', 1)[1].split(', ')[:6] if ': ' in line else []
            unknown[word] = [suggestion for suggestion in suggestions if len(suggestion) <= 80]
        elif not line.startswith(('*', '+', '-')):
            raise RuntimeError('Не удалось прочитать ответ словаря.')
    stems = {}
    for line in _run('-s', words).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in words:
            stems.setdefault(parts[0], set()).update(parts[1:])
    return unknown, stems


def check(text, ignored=()):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARACTERS:
        raise ValueError('Для корректора нужен текст от 1 до 10 000 знаков.')
    if not LOCK.acquire(blocking=False):
        raise ValueError('Корректор занят. Попробуйте через несколько секунд.')
    try:
        links = [(match.start(), match.end()) for match in LINK.finditer(text)]
        def protected(start, end):
            return any(start < right and end > left for left, right in links)
        tokens = [match for match in WORD.finditer(text) if not protected(match.start(), match.end())]
        ignored = {word.casefold() for word in ignored}
        eligible = list(dict.fromkeys(match.group() for match in tokens
                                     if 2 <= len(match.group()) <= 40 and match.group().casefold() not in ignored))
        words = eligible[:MAX_WORDS]
        unknown, stems, warnings = {}, {}, []
        if len(eligible) > MAX_WORDS:
            warnings.append('Достигнут предел 800 разных словоформ: оставшиеся слова не проверены словарём.')
        engine = available()
        if engine and words:
            try:
                unknown, stems = _dictionary(words)
            except (RuntimeError, subprocess.TimeoutExpired, OSError):
                engine = False
                warnings.append('Словарь не завершил проверку. Показаны только правила; повторите словарную проверку позже.')
        elif not engine:
            warnings.append('Словарный движок не установлен. Доступны только правила.')
        issues = []
        def add(start, end, kind, message, suggestions=()):
            if len(issues) >= MAX_ISSUES or protected(start, end):
                return
            issues.append({'start': len(text[:start].encode('utf-16-le')) // 2,
                           'end': len(text[:end].encode('utf-16-le')) // 2,
                           'original': text[start:end], 'kind': kind, 'message': message,
                           'suggestions': list(suggestions)})
        # Rule findings take priority over repeated occurrences of unknown words.
        for match in re.finditer(r'(?<=\S)[ \t]{2,}(?=\S)', text):
            add(*match.span(), 'spacing', 'Несколько пробелов между словами.', [' '])
        for match in re.finditer(r'[ \t]+(?=[,;:!?])', text):
            add(*match.span(), 'spacing', 'Пробел перед знаком препинания.', [''])
        for match in re.finditer(r'[,;:](?=[А-Яа-яЁё])', text):
            add(*match.span(), 'punctuation', 'Возможно, после знака нужен пробел.', [match.group() + ' '])
        for match in re.finditer(r'([,;:])\1+', text):
            add(*match.span(), 'punctuation', 'Повтор знака препинания.', [match.group()[0]])
        for match in re.finditer(r'\b([А-Яа-яЁёA-Za-z]+)([ \t]+)\1\b', text, re.I):
            add(*match.span(), 'repetition', 'Возможный случайный повтор слова.', [match.group(1)])
        for match in re.finditer(r'\b(хочу|хочешь|хочет|хотим|хотите|хотят|нужно|надо|можно|должен|должна|должны|должно)\s+([А-Яа-яЁё]{2,}тся)\b', text, re.I):
            word = match.group(2)
            add(*match.span(2), 'grammar', 'После этого слова обычно нужен инфинитив на «-ться». Проверьте смысл фразы.', [word[:-3] + 'ться'])
        for match in re.finditer(r'\bболее[ \t]+(лучше|хуже)\b', text, re.I):
            replacement = match.group(1)
            if match.group()[0].isupper():replacement = replacement.capitalize()
            add(*match.span(), 'grammar', 'Возможна лишняя сравнительная форма «более».', [replacement])
        for match in re.finditer(r'\bв[ \t]+течении(?=[ \t]+(?:дня|недели|месяца|года|часа|минуты|секунды|суток)\b)', text, re.I):
            add(*match.span(), 'grammar', 'В значении времени обычно пишется «в течение».', [match.group()[:-1] + 'е'])
        stack = []
        pairs = {')': '(', ']': '[', '»': '«'}
        for index, char in enumerate(text):
            if protected(index, index+1):
                continue
            if char in pairs.values():
                stack.append((char, index))
            elif char in pairs:
                if stack and stack[-1][0] == pairs[char]:
                    stack.pop()
                else:
                    add(index, index+1, 'punctuation', 'Проверьте парную скобку или кавычку.')
        for char, index in stack:
            add(index, index+1, 'punctuation', 'Не найдена закрывающая скобка или кавычка.')
        for match in tokens:
            if match.group() in unknown:
                add(*match.span(), 'spelling', 'Словарь не узнал слово: возможна опечатка, имя или специальный термин.', unknown[match.group()])
        groups = {}
        for match in tokens:
            word = match.group().lower()
            candidates = stems.get(match.group(), set())
            # Ambiguous roots stay separate; no arbitrary linguistic decision.
            stem = next(iter(candidates)).lower() if len(candidates) == 1 else word
            group = groups.setdefault(stem, {'word': stem, 'count': 0, 'forms': Counter(), 'dictionary_stem': len(candidates) == 1})
            group['count'] += 1
            group['forms'][word] += 1
        frequent = [{**item, 'forms': [{'word': form, 'count': count} for form, count in item['forms'].most_common()]}
                    for item in sorted(groups.values(), key=lambda item: (-item['count'], item['word']))[:30]]
        return {'text_sha256': sha256(text.encode()).hexdigest(), 'offset_encoding': 'utf-16',
                'issues': sorted(issues, key=lambda item: (item['start'], item['end'])),
                'dictionary_available': engine, 'checked_unique_forms': len(words) if engine else 0,
                'unique_forms_limit': MAX_WORDS, 'issues_limit': MAX_ISSUES,
                'issues_truncated': len(issues) == MAX_ISSUES,
                'frequencies': frequent, 'warnings': warnings,
                'limitations': 'Русский словарь и простые правила. Полная грамматика, смысл и факты не проверяются. Подсказки требуют вашего решения.'}
    finally:
        LOCK.release()
