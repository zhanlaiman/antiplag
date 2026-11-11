"""Local, evidence-based comparison against the sources supplied by the user.

Offsets refer to Python characters in the original strings. Similar matches are
lexical candidates for review, not claims about authorship or semantic copying.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


MAX_TEXT_CHARS = 200_000
MAX_SOURCES = 100
MATCHER_VERSION = "lexical-v2-kind-aware-dedup"
MIN_WORDS = 6
MIN_CHARACTERS = 35
# These combining marks cover Cyrillic stress and Latin/Cyrillic diacritics.
# Attached marks remain part of a token when Unicode cannot compose them.
WORD_PATTERN = re.compile(r"[^\W_](?:[^\W_]|[\u0300-\u036f])*", re.UNICODE)
CLUSTER_PATTERN = re.compile(r".[\u0300-\u036f]*", re.DOTALL)
INVISIBLE = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
CYRILLIC_STRESS = str.maketrans({"\u0300": None, "\u0301": None})
CONFUSABLES = str.maketrans("aceopxykmtbh", "асеорхукмтвн")


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


def _tokens(text):
    characters = []
    offsets = []
    end_offsets = []
    for cluster in CLUSTER_PATTERN.finditer(text):
        original = "".join(character for character in cluster.group() if character not in INVISIBLE)
        if not original:
            continue
        normalized = unicodedata.normalize("NFD", original)
        if re.search(r"[А-Яа-яЁё]", normalized):
            normalized = normalized.translate(CYRILLIC_STRESS)
        normalized = unicodedata.normalize("NFKC", normalized).lower().replace("ё", "е")
        characters.extend(normalized)
        offsets.extend([cluster.start()] * len(normalized))
        end_offsets.extend([cluster.end()] * len(normalized))
    normalized_text = "".join(characters)
    tokens = []
    for word in WORD_PATTERN.finditer(normalized_text):
        value = word.group()
        if re.search(r"[а-я]", value):
            value = value.translate(CONFUSABLES)
        tokens.append(Token(value, offsets[word.start()], end_offsets[word.end() - 1]))
    return tokens


def normalize(text):
    """Normalize typography while preserving original evidence offsets.

    Cyrillic acute/grave stress marks are ignored. Other diacritics, including
    the breve in й and Latin accents, retain their canonical Unicode meaning.
    """
    return " ".join(token.value for token in _tokens(text))


def _issues(text):
    issues = []
    if any(character in INVISIBLE for character in text):
        issues.append({"kind": "invisible_characters", "message": "Обнаружены невидимые символы. При сравнении они удалены."})
    for word in WORD_PATTERN.finditer(text):
        if re.search(r"[а-яё]", word.group(), re.I) and re.search(r"[a-z]", word.group(), re.I):
            issues.append({"kind": "mixed_scripts", "message": "В одном слове смешаны кириллица и латиница. Похожие символы учтены при сравнении."})
            break
    return issues


def _quoted(text, start, end):
    prefix = text[:start]
    suffix = text[end:]
    return (prefix.rfind("«") > prefix.rfind("»") and "»" in suffix) or (
        prefix.count('"') % 2 == 1 and '"' in suffix
    )


def _make_match(text, source, target_tokens, source_tokens, target_start,
                target_end, source_start, source_end, kind, score):
    start = target_tokens[target_start].start
    end = target_tokens[target_end - 1].end
    other_start = source_tokens[source_start].start
    other_end = source_tokens[source_end - 1].end
    return {
        "source_id": source["id"],
        "source_title": source.get("title", "Источник"),
        "source_url": source.get("url", ""),
        "start": start,
        "end": end,
        "source_start": other_start,
        "source_end": other_end,
        "excerpt": text[start:end],
        "source_excerpt": source["text"][other_start:other_end],
        "kind": kind,
        "score": round(score, 4),
        "quoted": _quoted(text, start, end),
    }


def _exact_matches(text, source, target_tokens, source_tokens):
    index = defaultdict(list)
    source_words = [token.value for token in source_tokens]
    target_words = [token.value for token in target_tokens]
    for position in range(len(source_words) - MIN_WORDS + 1):
        key = tuple(source_words[position:position + MIN_WORDS])
        # One representative of repeated wording is sufficient as evidence.
        if len(index[key]) < 64:
            index[key].append(position)
    matches = []
    position = 0
    while position <= len(target_words) - MIN_WORDS:
        candidates = index.get(tuple(target_words[position:position + MIN_WORDS]), ())
        best_length = 0
        best_source_position = 0
        for source_position in candidates:
            length = MIN_WORDS
            while (position + length < len(target_words)
                   and source_position + length < len(source_words)
                   and target_words[position + length] == source_words[source_position + length]):
                length += 1
            if length > best_length:
                best_length = length
                best_source_position = source_position
            if length == len(target_words) - position:
                break
        if best_length:
            match = _make_match(text, source, target_tokens, source_tokens,
                                position, position + best_length,
                                best_source_position, best_source_position + best_length,
                                "exact", 1.0)
            if match["end"] - match["start"] >= MIN_CHARACTERS:
                matches.append(match)
            position += max(1, best_length - MIN_WORDS + 1)
        else:
            position += 1
    return _deduplicate(matches)


def _segments(text, tokens):
    """Bound fuzzy comparison to sentence windows of at most 120 words."""
    segments = []
    start = 0
    for position, token in enumerate(tokens):
        gap = text[tokens[position - 1].end:token.start] if position else ""
        boundary = bool(re.search(r"[.!?\n]", gap))
        if position > start and (boundary or position - start >= 120):
            if position - start >= MIN_WORDS:
                segments.append((start, position))
            start = position
    if len(tokens) - start >= MIN_WORDS:
        segments.append((start, len(tokens)))
    return segments


def _similar_matches(text, source, target_tokens, source_tokens, exact):
    source_segments = _segments(source["text"], source_tokens)
    segment_words = [[token.value for token in source_tokens[start:end]]
                     for start, end in source_segments]
    inverted = defaultdict(list)
    for number, words in enumerate(segment_words):
        for word in set(words):
            inverted[word].append(number)
    matches = []
    for start, end in _segments(text, target_tokens):
        character_start = target_tokens[start].start
        character_end = target_tokens[end - 1].end
        covered = _coverage([
            {"start": max(character_start, match["start"]), "end": min(character_end, match["end"])}
            for match in exact
            if match["start"] < character_end and match["end"] > character_start
        ])
        if covered / (character_end - character_start) >= 0.75:
            continue
        words = [token.value for token in target_tokens[start:end]]
        overlaps = Counter(number for word in set(words) for number in inverted.get(word, ()))
        for number, shared_count in overlaps.most_common(8):
            other_words = segment_words[number]
            if shared_count < MIN_WORDS or shared_count / max(len(set(words)), len(set(other_words))) < 0.55:
                continue
            matcher = SequenceMatcher(None, words, other_words, autojunk=False)
            ratio = matcher.ratio()
            if 0.78 <= ratio < 1:
                source_start, source_end = source_segments[number]
                match = _make_match(text, source, target_tokens, source_tokens,
                                    start, end, source_start, source_end, "similar", ratio)
                if match["end"] - match["start"] >= MIN_CHARACTERS:
                    matches.append(match)
                    break
    return matches


def _deduplicate(matches):
    selected = []
    for match in sorted(matches, key=lambda item: (item["start"], -item["end"])):
        if any(previous["kind"] == match["kind"] and previous["start"] <= match["start"]
               and previous["end"] >= match["end"] for previous in selected):
            continue
        selected.append(match)
    return selected


def _coverage(matches):
    total = 0
    previous_end = 0
    for match in sorted(matches, key=lambda item: item["start"]):
        total += max(0, match["end"] - max(previous_end, match["start"]))
        previous_end = max(previous_end, match["end"])
    return total


def analyze(text, sources):
    """Compare a document with supplied sources and return grounded evidence.

    Character coverage includes whitespace inside matched spans, counts overlaps
    once, and uses the entire original document as the denominator.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Добавьте текст для проверки.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"Текст должен содержать не больше {MAX_TEXT_CHARS} символов.")
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"За одну проверку можно сравнить до {MAX_SOURCES} источников.")
    target_tokens = _tokens(text)
    matches = []
    for source in sources:
        if not isinstance(source.get("text"), str):
            raise ValueError("У источника отсутствует текст.")
        if len(source["text"]) > MAX_TEXT_CHARS:
            raise ValueError(f"Источник должен содержать не больше {MAX_TEXT_CHARS} символов.")
        source_tokens = _tokens(source["text"])
        exact = _exact_matches(text, source, target_tokens, source_tokens)
        similar = _similar_matches(text, source, target_tokens, source_tokens, exact)
        matches.extend(_deduplicate(similar + exact))
    matches.sort(key=lambda match: (match["start"], -match["score"], str(match["source_id"])))
    for number, match in enumerate(matches, 1):
        match["id"] = f"match-{number}"
    matched_characters = _coverage(matches)
    return {
        "matches": matches,
        "stats": {
            "matched_characters": matched_characters,
            "total_characters": len(text),
            "similarity_percent": round(100 * matched_characters / len(text), 1),
            "source_count": len({match["source_id"] for match in matches}),
            "checked_source_count": len(sources),
        },
        "issues": _issues(text),
    }
