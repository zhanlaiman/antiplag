"""Local-only frozen metadata hypothesis: exact document runs, not query runs.

Independent title/chunks are ordering hints. No source bodies, labels, URLs from
an answer key, I/O, or network are accepted by the selector.
"""
from plagiarism.matching import MAX_TEXT_CHARS, _tokens
from scripts.probe_search_metadata import METADATA_VERSION, _CHUNK_BREAK, retain_results

STRATEGY = "document-metadata-runs-6-to-10-local-v1"
MIN_RUN = 6
MAX_RUN = 10
MAX_DOCUMENT_TOKENS = 50000
MAX_NGRAM_ENTRIES = 250000


def document_index(text):
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        return None, "document_character_bound"
    words = [token.value for token in _tokens(text)]
    if len(words) > MAX_DOCUMENT_TOKENS:
        return None, "document_token_bound"
    vocabulary = {word: index + 1 for index, word in enumerate(dict.fromkeys(words))}
    identifiers = [vocabulary[word] for word in words]
    indexes = {length: {tuple(identifiers[start:start + length]) for start in range(max(0, len(words) - length + 1))}
               for length in range(MIN_RUN, MAX_RUN + 1)}
    if sum(map(len, indexes.values())) > MAX_NGRAM_ENTRIES:
        return None, "document_ngram_bound"
    return {"vocabulary": vocabulary, "ngrams": indexes, "tokens": len(words),
            "entries": sum(map(len, indexes.values()))}, None


def longest_indexed_run(index, value):
    words = [index["vocabulary"].get(token.value, 0) for token in _tokens(value)]
    for length in range(MAX_RUN, MIN_RUN - 1, -1):
        if any(tuple(words[start:start + length]) in index["ngrams"][length]
               for start in range(max(0, len(words) - length + 1))):
            return length
    return 0


def rank_document_metadata(base_order, text, records, *, projected=False):
    """Build the 6..10-token index ONCE, then scan bounded metadata windows."""
    if len(base_order) != len(set(base_order)) or not isinstance(records, list) or len(records) > 6:
        raise ValueError("Deduplicated discovery order and at most six groups required")
    index, fallback = document_index(text)
    audit = {"strategy": STRATEGY, "minimum_words": MIN_RUN, "maximum_words": MAX_RUN,
             "index_built_once": True, "index_tokens": index["tokens"] if index else None,
             "index_entries": index["entries"] if index else None, "fallback_reason": fallback,
             "reasons": {}, "may_be_used_as_matching_evidence": False}
    if fallback:
        return list(base_order), audit
    scores, allowed = {}, set(base_order)
    for query_index, record in enumerate(records):
        values = record.get("results")
        projected_values = retain_results(values)
        if projected:
            if (len(projected_values) != len(values) or any(value.get("metadata_version") != METADATA_VERSION
                    or value.get("metadata_purpose") != "ranking_only" or type(value.get("provider_rank")) is not int
                    or not 1 <= value["provider_rank"] <= 5 for value in values)):
                raise ValueError("Bounded projected metadata identity required")
            ranks = [value["provider_rank"] for value in values]
            if ranks != sorted(set(ranks)):
                raise ValueError("Original projected ranks must retain order")
            for original, value in zip(values, projected_values):
                value["provider_rank"] = original["provider_rank"]
        for result in projected_values:
            url = result["url"]
            if url not in allowed:
                continue
            views = [result.get("title", "")] + _CHUNK_BREAK.split(result.get("content", ""))
            run = max((longest_indexed_run(index, value) for value in views), default=0)
            if run >= MIN_RUN and run > scores.get(url, 0):
                scores[url] = run
                audit["reasons"][url] = {"longest_contiguous_document_words": run,
                    "query_index": query_index, "provider_rank": result["provider_rank"],
                    "purpose": "ordering_only", "page_content_verified": False,
                    "may_be_used_as_matching_evidence": False}
    return sorted(base_order, key=lambda url: -scores.get(url, 0)), audit
