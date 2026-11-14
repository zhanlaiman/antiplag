"""Shorten one search phrase at an explicit clause boundary, using text only."""
from copy import deepcopy
import re

from plagiarism.matching import MIN_WORDS, _tokens

CLAUSE_STRATEGY = "hybrid-citation-v7-clause-boundaries"
_BOUNDARY = re.compile(r"[:;](?=\s)|(?<=\s)[—–](?=\s)")


def clause_boundary_queries(text, plan, protected_queries, *, limit=6):
    """Keep count/order and quoted citations; trim at most one existing phrase.

    A ten-word query may straddle an introductory clause and a short insert.
    Only its trailing clause is retained, with at least the existing six-word
    search minimum. This changes neither matching thresholds nor the budget.
    """
    planned = deepcopy(list(plan))
    if limit != 6:
        return planned
    protected = set(protected_queries)
    for index, item in enumerate(planned):
        if item["query"] in protected:
            continue
        fragment = text[item["start"]:item["end"]]
        words = _tokens(fragment)
        if len(words) <= MIN_WORDS:
            continue
        boundaries = list(_BOUNDARY.finditer(fragment))
        if not boundaries:
            continue
        boundary = boundaries[-1]
        before = [word for word in words if word.end <= boundary.start()]
        after = [word for word in words if word.start >= boundary.end()]
        # Times and numeric ranges are not introductory clauses.
        if (not before or len(after) < MIN_WORDS
                or (before[-1].value.isdecimal() and after[0].value.isdecimal())):
            continue
        query = '"' + " ".join(word.value[:40] for word in after) + '"'
        if len(query) > 390 or query in {q["query"] for i, q in enumerate(planned) if i != index}:
            continue
        planned[index] = {**item, "query": query,
                          "start": item["start"] + after[0].start,
                          "end": item["start"] + after[-1].end}
        break
    return planned
