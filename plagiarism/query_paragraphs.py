"""Text-only, opt-in replacement of one redundant primary search phrase.

The caller supplies the completed DENSE plan and protected citation strings.
No source labels, search results, transports, or web configuration enter here.
"""
from copy import deepcopy
from fractions import Fraction
import re

from plagiarism.matching import _tokens

PARAGRAPH_STRATEGY = "hybrid-citation-v5-paragraph-redundancy"


def _paragraphs(text):
    blocks = []
    cursor = 0
    for separator in re.finditer(r"\n[ \t\r\n]*\n", text):
        blocks.append((cursor, separator.start()))
        cursor = separator.end()
    blocks.append((cursor, len(text)))
    result = []
    for start, end in blocks:
        tokens = _tokens(text[start:end])
        if tokens:
            result.append({"start": start + tokens[0].start,
                           "end": start + tokens[-1].end,
                           "offset": start, "tokens": tokens})
    return result


def paragraph_redundancy_queries(text, dense_plan, protected_queries, *, limit=6):
    """Return a new plan using the frozen short-paragraph redundancy rule.

    Only the six-query ceiling was evaluated. A smaller requested limit keeps
    the supplied DENSE plan unchanged. A short plan at limit=6 still follows
    the same eligibility rules; its actual count and order never change.
    """
    planned = deepcopy(list(dense_plan))
    if limit != 6:
        return planned
    paragraphs = _paragraphs(text)

    def contained(query, paragraph):
        return paragraph["start"] <= query["start"] and query["end"] <= paragraph["end"]

    eligible = [paragraph for paragraph in paragraphs
                if 10 <= len(paragraph["tokens"]) <= 40
                and not any(contained(query, paragraph) for query in planned)]
    if not eligible:
        return planned
    target = eligible[0]
    window = target["tokens"][:10]
    query = '"' + " ".join(token.value[:40] for token in window) + '"'
    if len(query) > 390 or query in {item["query"] for item in planned}:
        return planned
    protected = set(protected_queries)
    words = [{token.value for token in _tokens(item["query"])
              if len(token.value) >= 4 and not token.value.isdecimal()}
             for item in planned]
    replacements = []
    for index, item in enumerate(planned):
        if item["query"] in protected:
            continue
        if any(len(paragraph["tokens"]) > 40 and contained(item, paragraph)
               and sum(contained(other, paragraph) for other in planned) >= 2
               for paragraph in paragraphs):
            redundancy = max((Fraction(len(words[index] & other), max(1, len(words[index])))
                              for other_index, other in enumerate(words) if other_index != index),
                             default=Fraction(0, 1))
            replacements.append((redundancy, index))
    if not replacements:
        return planned
    replacement = max(replacements)[1]
    modified = deepcopy(planned)
    modified[replacement] = {"query": query,
                             "start": target["offset"] + window[0].start,
                             "end": target["offset"] + window[-1].end}
    assert len(modified) == len(planned)
    assert len({item["query"] for item in modified}) == len(modified)
    assert all(item in modified for item in planned if item["query"] in protected)
    assert sum(before != after for before, after in zip(planned, modified)) == 1
    return modified
