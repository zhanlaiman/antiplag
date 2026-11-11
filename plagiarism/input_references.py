"""Extract bounded, unverified source hints explicitly named in submitted text.

This parser performs no I/O and makes no claim that a named article supports the
text. Only the measured parenthetical Wikipedia/article/title syntax is used.
Offsets are Python character positions in the original submitted string.
"""

import re
import unicodedata
from urllib.parse import quote

from .matching import MAX_TEXT_CHARS


MAX_NAMED_REFERENCES = 6
_PARENTHETICAL = re.compile(r"\(([^()\r\n]{1,300})\)")
_QUOTE = r'«([^«»]+)»|“([^“”]+)”|„([^„“]+)“|"([^"\n]+)"'
_TITLE = re.compile(r"\b(?:статья|статье|article)\s*[:—–-]?\s*(?:" + _QUOTE + ")", re.I)
_WIKIPEDIA = re.compile(r"\b(?:Википедия|Russian\s+Wikipedia)\b", re.I)
_NEGATION = re.compile(r"\b(?:не|not)\b", re.I)


def extract_named_references(text):
    """Return at most six distinct, input-derived Russian Wikipedia URLs.

    Each item has ``url``, whitespace-normalized ``title``, original
    ``citation_span`` and ``title_span`` pairs, and ``source_claim`` equal to
    ``unverified_input_reference``. The first occurrence supplies the spans.
    A citation can name an incorrect article; loading and matching are separate.
    """
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise ValueError("Named references require text within the matching input bound.")
    references, seen = [], set()
    # Each candidate has at most 300 characters. No rescans of preceding text
    # or attempts to attach quoted passages are needed for source discovery.
    for citation in _PARENTHETICAL.finditer(text):
        content = citation.group(1)
        title = _TITLE.search(content)
        if title is None or _WIKIPEDIA.search(content) is None:
            continue
        group = next(index for index in range(1, 5) if title.group(index) is not None)
        start, end = title.span(group)
        without_title = content[:start] + " " * (end - start) + content[end:]
        if _NEGATION.search(without_title):
            continue
        normalized = " ".join(content[start:end].split())
        if (not normalized or normalized in {".", ".."}
                or any(unicodedata.category(character).startswith("C") for character in normalized)):
            continue
        url = "https://ru.wikipedia.org/wiki/" + quote(normalized.replace(" ", "_"), safe="")
        if url in seen:
            continue
        seen.add(url)
        offset = citation.start(1)
        references.append({"url": url, "title": normalized,
                           "citation_span": list(citation.span()),
                           "title_span": [offset + start, offset + end],
                           "source_claim": "unverified_input_reference"})
        if len(references) == MAX_NAMED_REFERENCES:
            break
    return references
