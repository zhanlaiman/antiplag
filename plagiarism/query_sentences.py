"""Cover a short sentence instead of repeating an overlapping search window."""
from copy import deepcopy
from fractions import Fraction
import re

from plagiarism.matching import MIN_WORDS, _tokens

SHORT_SENTENCE_STRATEGY = "hybrid-citation-v9-short-sentences"


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
