#!/usr/bin/env python3
"""Local prototype: retain bounded discovery metadata and rank saved URLs.

No provider, fetcher, model, file reader or report writer exists here. Snippets
are unverified ordering hints and must never be passed to the matcher as pages.
"""
import math
import re

from plagiarism.matching import _tokens
from plagiarism.search_tavily import _url

METADATA_VERSION = "tavily-ranking-metadata-prototype-v1"
MAX_TITLE_CHARACTERS = 300
MAX_CONTENT_CHARACTERS = 4000
MAX_RESULTS = 5
MAX_QUERIES = 6
MIN_QUERY_RUN = 6
_CHUNK_BREAK = re.compile(r"\[\s*(?:\.{3}|…)\s*\]")


def bounded_text(value, limit, secret=None):
    if not isinstance(value, str) or (secret and secret in value):
        return None
    # Bound before tokenization. Surrogates/noncharacters/control bytes are not
    # useful ranking hints; missing optional metadata preserves URL discovery.
    prefix = value[:limit]
    if any(0xD800 <= ord(c) <= 0xDFFF or ord(c) & 0xFFFF in (0xFFFE, 0xFFFF)
           or 0xFDD0 <= ord(c) <= 0xFDEF or ord(c) < 32 and not c.isspace() for c in prefix):
        return None
    return prefix


def retain_results(values, secret=None):
    """Prototype response projection; never alter a request or consume credit."""
    if not isinstance(values, list) or len(values) > MAX_RESULTS:
        raise ValueError("Expected at most five Basic results")
    projected, seen = [], set()
    for rank, item in enumerate(values, 1):
        if not isinstance(item, dict):
            raise ValueError("Malformed search result")
        url = _url(item.get("url"))
        if secret and secret in url:
            raise ValueError("Reflected credential in result URL")
        if url in seen:
            continue
        seen.add(url)
        result = {"url": url, "provider_rank": rank, "metadata_purpose": "ranking_only",
                  "metadata_version": METADATA_VERSION}
        for name, limit in (("title", MAX_TITLE_CHARACTERS), ("content", MAX_CONTENT_CHARACTERS)):
            value = bounded_text(item.get(name), limit, secret)
            if value is not None:
                result[name] = value
                result[name + "_truncated"] = len(item[name]) > limit
        score = item.get("score")
        if type(score) in (int, float) and math.isfinite(score) and 0 <= score <= 1:
            result["score"] = score
        projected.append(result)
    return projected


def longest_run(query_words, source_words):
    """Longest ordered contiguous run; unmatched words cannot bridge a gap."""
    previous, longest = [0] * (len(query_words) + 1), 0
    for source_word in source_words:
        current = [0]
        for index, word in enumerate(query_words, 1):
            value = previous[index - 1] + 1 if word == source_word else 0
            current.append(value)
            longest = max(longest, value)
        previous = current
    return longest


def rank_by_snippet_overlap(base_order, records, *, projected=False):
    """Stably rank already discovered URLs by their own query/snippet overlap.

    The provider score is preserved for audit, not compared across queries.
    Missing or weak hints retain the original order. Titles and content chunks
    are independent views: concatenation must not invent phrase adjacency.
    """
    if not isinstance(records, list) or len(records) > MAX_QUERIES:
        raise ValueError("Expected at most six frozen query groups")
    if len(base_order) != len(set(base_order)):
        raise ValueError("Base discovery order must already be deduplicated")
    allowed, scores, reasons = set(base_order), {}, {}
    for query_index, record in enumerate(records):
        query = record.get("query")
        if not isinstance(query, str) or len(query) > 400:
            raise ValueError("Bounded query required")
        query_words = [t.value for t in _tokens(query)]
        if len(query_words) > 40:
            raise ValueError("Unexpectedly long query token sequence")
        values = record.get("results")
        projected_values = retain_results(values)
        if projected:
            if (len(projected_values) != len(values) or any(
                    value.get("metadata_version") != METADATA_VERSION or value.get("metadata_purpose") != "ranking_only"
                    or type(value.get("provider_rank")) is not int or not 1 <= value["provider_rank"] <= MAX_RESULTS
                    for value in values)):
                raise ValueError("Bounded projected metadata identity required")
            ranks = [value["provider_rank"] for value in values]
            if ranks != sorted(set(ranks)):
                raise ValueError("Original projected provider ranks must retain their order")
            for original, result in zip(values, projected_values):
                result["provider_rank"] = original["provider_rank"]
        for result in projected_values:
            url = result["url"]
            if url not in allowed:
                continue
            views = [result.get("title", "")]
            views.extend(_CHUNK_BREAK.split(result.get("content", "")))
            run = max((longest_run(query_words, [t.value for t in _tokens(view)]) for view in views), default=0)
            if run >= MIN_QUERY_RUN and run > scores.get(url, 0):
                scores[url] = run
                reasons[url] = {"query_index": query_index, "provider_rank": result["provider_rank"],
                    "longest_contiguous_query_words": run, "purpose": "ordering_only",
                    "page_content_verified": False, "may_be_used_as_matching_evidence": False}
    return sorted(base_order, key=lambda url: -scores.get(url, 0)), reasons
