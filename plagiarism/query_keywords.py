"""Keep one complete, explicitly labelled keyword field as a discovery clue."""
from copy import deepcopy
import re

from plagiarism.matching import MIN_WORDS, _tokens

KEYWORD_STRATEGY = "hybrid-citation-v8-complete-keywords"
_FIELD = re.compile(r"^[ \t]*(?:ключевые[ \t]+слова|key[ \t]*words)[ \t]*:[ \t]*([^\r\n]+)", re.I | re.M)


def complete_keyword_queries(text, plan, protected_queries=(), *, limit=6):
    """Replace at most one truncated metadata phrase, retaining the same slots.

    Keyword fields describe concepts rather than a literal quotation. Preserve
    the whole bounded field in its original order without outer query quotes.
    No title, domain or external reference is inferred or added.
    """
    planned = deepcopy(list(plan))
    if limit != 6:
        return planned
    protected = set(protected_queries)
    for field in _FIELD.finditer(text):
        fragment = field.group(1)
        if len(fragment) > 390:
            continue
        words = _tokens(fragment)
        if not MIN_WORDS <= len(words) <= 30:
            continue
        query = " ".join(word.value[:40] for word in words)
        if len(query) > 390 or any(query == item["query"].strip('"') for item in planned):
            continue
        start, end = field.start(1) + words[0].start, field.start(1) + words[-1].end
        for index, item in enumerate(planned):
            if (index == 0 or item["query"] in protected
                    or not start <= item["start"] < item["end"] <= end
                    or len(words) < len(_tokens(item["query"])) + 2):
                continue
            planned[index] = {**item, "query": query, "start": start, "end": end}
            return planned
    return planned
