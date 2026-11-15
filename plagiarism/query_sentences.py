"""Cover a short sentence instead of repeating an overlapping search window."""
from copy import deepcopy
from fractions import Fraction
import re

from plagiarism.matching import MIN_WORDS, _tokens

SHORT_SENTENCE_STRATEGY = "hybrid-citation-v9-short-sentences"
SHORT_CENTER_STRATEGY = "hybrid-citation-v10-short-center"
SHORT_PURE_STRATEGY = "hybrid-citation-v11-short-pure"
SHORT_SUFFIX_STRATEGY = "hybrid-citation-v13-empty-nine-suffix-six"
INPUT_QUERY_POLICY = "least-covered-pure-short-sentence-v1"
SUFFIX_QUERY_POLICY = "empty-nine-suffix-six-v1"
TITLE_QUERY_POLICY = "single-document-observed-title-v1"


def short_sentence_queries(text, plan, protected_queries, *, limit=6):
    """Replace at most one redundant slot, retaining citations and the first query.

    Ten-word windows can miss a complete six-to-nine-word sentence even in a
    short document. Its first six words are sufficient as a discovery clue;
    matching still requires evidence from a fetched page. Selection uses only
    input text and offsets. The six-query allowance is unchanged.
    """
    planned = deepcopy(list(plan))
    if limit != 6:
        return planned
    tokens = _tokens(text)
    segments, start = [], 0
    for index in range(1, len(tokens)):
        if re.search(r"[.!?…\r\n]", text[tokens[index - 1].end:tokens[index].start]):
            segments.append(tokens[start:index])
            start = index
    if tokens:
        segments.append(tokens[start:])

    def overlap(words, item):
        return sum(item['start'] <= word.start and word.end <= item['end'] for word in words)

    candidates = [words for words in segments
                  if MIN_WORDS <= len(words) < 10
                  and all(overlap(words, item) < MIN_WORDS for item in planned)]
    protected = set(protected_queries)
    # A keyword field and explicit quotation already contain useful metadata.
    # They are protected by the caller even when they occupy a later slot.
    redundant = []
    for index, item in enumerate(planned):
        if index == 0 or item['query'] in protected:
            continue
        words = [word for word in tokens if item['start'] <= word.start and word.end <= item['end']]
        if not words:
            continue
        ratio = max((Fraction(overlap(words, other), len(words))
                     for other_index, other in enumerate(planned) if other_index != index), default=Fraction(0))
        if ratio >= Fraction(1, 2):
            redundant.append((ratio, index))
    if not redundant:
        return planned
    for words in candidates:
        window = words[:MIN_WORDS]
        phrase = '"' + ' '.join(word.value[:40] for word in window) + '"'
        if len(phrase) > 390 or phrase in {item['query'] for item in planned}:
            continue
        replacement = max(redundant)[1]
        planned[replacement] = {'query': phrase, 'start': window[0].start, 'end': window[-1].end}
        break
    return planned


def short_center_queries(text, plan, *, limit=6):
    """Use one spare slot for a shorter middle window in a short document.

    Dense sentence windows can produce fewer than six unique phrases. Keep
    every existing phrase and use only that unused allowance. A PDF line-wrap
    hyphen is removed in the search clue; original offsets/text/evidence stay
    intact. No matcher normalization or minimum evidence length changes.
    """
    planned = deepcopy(list(plan))
    if limit != 6 or len(planned) >= limit:
        return planned
    words = _tokens(text)
    if not MIN_WORDS <= len(words) <= 160:
        return planned
    first = round((len(words) - MIN_WORDS) / 2)
    window = words[first:first + MIN_WORDS]
    fragment = text[window[0].start:window[-1].end]
    fragment = re.sub(r'(?<=[^\W\d_])[ \t]*-[ \t]*\r?\n[ \t]*(?=[^\W\d_])', '', fragment)
    terms = _tokens(fragment)
    if len(terms) < 4:
        return planned
    phrase = '"' + ' '.join(word.value[:40] for word in terms) + '"'
    if len(phrase) <= 390 and phrase not in {item['query'] for item in planned}:
        # Keep the last original slot available for an observed-document clue.
        planned.insert(max(1, len(planned) - 1),
                       {'query': phrase, 'start': window[0].start, 'end': window[-1].end})
    return planned


def short_pure_candidate(text, plan, protected_queries=(), *, limit=6, for_empty_suffix=False):
    """Propose one whole short sentence lacking a pure discovery phrase.

    This reallocates the final slot; it does not claim that slot was redundant.
    The caller retains valid observed-title priority. Only input words and their
    original positions participate in selection, never a source or its metadata.
    Empty-suffix mode instead selects nine-word sentences even if another clue
    already covered them; its caller must first verify five empty replies.
    """
    if limit != 6 or len(plan) != 6:
        return None
    protected = set(protected_queries)
    final = plan[-1]
    if not final['query'].startswith('"') or final['query'] in protected:
        return None
    tokens = _tokens(text)
    if not MIN_WORDS <= len(tokens) <= 160:
        return None
    segments, start = [], 0
    for index in range(1, len(tokens)):
        if re.search(r"[.!?…\r\n]", text[tokens[index - 1].end:tokens[index].start]):
            segments.append(tokens[start:index])
            start = index
    segments.append(tokens[start:])

    # Mere overlap can include words from another sentence. Require the whole
    # actual query and its original span to describe a contiguous input clue.
    pure_spans = []
    for item in plan:
        if not (item['query'].startswith('"') and item['query'].endswith('"')):
            continue
        query_words = _tokens(item['query'])
        span_words = [t for t in tokens if item['start'] <= t.start and t.end <= item['end']]
        if (len(query_words) >= MIN_WORDS and span_words
                and item['start'] == span_words[0].start and item['end'] == span_words[-1].end
                and [t.value for t in query_words] == [t.value for t in span_words]):
            pure_spans.append((item['start'], item['end']))
    if for_empty_suffix:
        # Earlier clues have empty replies; the final slot is still unattempted.
        # Preserve a pure clue already waiting in that slot.
        pure_spans = [span for span in pure_spans if span == (final['start'], final['end'])]
    candidates = []
    existing = {item['query'] for item in plan}
    for words in segments:
        if not MIN_WORDS <= len(words) <= 9 or any(len(w.value) > 40 for w in words):
            continue
        if for_empty_suffix and len(words) != 9:
            continue
        a, b = words[0].start, words[-1].end
        if any(a <= left and right <= b for left, right in pure_spans):
            continue
        phrase = '"' + ' '.join(w.value for w in words) + '"'
        if len(phrase) > 390 or (not for_empty_suffix and phrase in existing):
            continue
        # A prior full/prefix clue with an empty reply is not discovery evidence.
        # Suffix mode may shorten it, but must never repeat an existing query.
        if for_empty_suffix:
            suffix = '"' + ' '.join(w.value for w in words[-MIN_WORDS:]) + '"'
            if suffix in existing:
                continue
        covered = sum(any(q['start'] <= w.start and w.end <= q['end'] for q in plan[:5])
                      for w in words)
        candidates.append((Fraction(covered, len(words)), a, {
            'query': phrase, 'start': a, 'end': b,
            'covered_words': covered, 'sentence_words': len(words)}))
    return min(candidates, key=lambda item: item[:2])[2] if candidates else None


def short_suffix_candidate(text, plan, protected_queries=(), *, limit=6):
    """Use the last six original words of a whole nine-word input sentence.

    The caller must establish five successful empty primary replies. This
    formats a discovery clue only; fetched evidence must still ground matches.
    """
    whole = short_pure_candidate(text, plan, protected_queries, limit=limit, for_empty_suffix=True)
    if whole is None or whole['sentence_words'] != 9:
        return None
    words = _tokens(text[whole['start']:whole['end']])[-MIN_WORDS:]
    query = '"' + ' '.join(word.value for word in words) + '"'
    if len(query) > 390 or query in {item['query'] for item in plan}:
        return None
    return {**whole, 'query': query,
            'start': whole['start'] + words[0].start,
            'end': whole['start'] + words[-1].end,
            'whole_sentence_span': {'start': whole['start'], 'end': whole['end']},
            'query_words': MIN_WORDS}


def observed_title_query(url, title):
    """A bounded observed title can locate an accessible copy of one document.

    This is a discovery clue, never matching evidence. Accept article/PDF
    locators only, not site roots, snippets, fabricated URLs, or generic labels.
    """
    from urllib.parse import urlsplit
    if (not isinstance(url, str) or not isinstance(title, str) or len(title) > 240
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in title)):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ('http','https') or not parsed.hostname:
        return None
    path = parsed.path.lower()
    if not (path.endswith('.pdf') or '/article/' in path or path.endswith('/item.asp')):
        return None
    # Search services commonly truncate display titles using an ellipsis.
    title = re.split(r'\.{3}|…', title, maxsplit=1)[0].strip()
    words = _tokens(title)
    if not 4 <= len(words) <= 20 or sum(len(w.value) >= 7 for w in words) < 2:
        return None
    phrase = '"' + ' '.join(w.value[:40] for w in words) + '"'
    return phrase if len(phrase) <= 390 else None
