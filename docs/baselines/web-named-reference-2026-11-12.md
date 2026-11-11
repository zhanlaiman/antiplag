# Explicit named-reference source probe, 2026-11-12

A direct URL derived from D14's own citation successfully loaded its source and confirmed the entire 15-word quotation. The previous saved D14 report had loaded five other pages, covered none of these quotation words exactly, and had not loaded this article. This experiment performs a separate source load; production discovery, queue selection, query planning, and defaults remain unchanged.

The new input-only parser accepts a parenthetical of at most 300 characters containing explicit `Википедия` or `Russian Wikipedia`, plus `статья`, `статье`, or `article` followed by a quoted title. It fixes the origin to `https://ru.wikipedia.org/wiki/`, converts title spaces to underscores, and percent-encodes the title. Explicit `не`/`not` outside the title rejects the reference. Input spans and raw title are retained. A derived URL is always an **unverified input reference**, not a claim that the article supports the text.

All 30 frozen DEV inputs were checked locally before the load. Only D14 produced a hint, directly from title span 361–371, `Метрология`. Eight authored checks covered another article, absent markers, absent explicit article wording, two negations, an English marker, a false attribution, and URL-like title text. All passed. The URL-like title remains an encoded path on the fixed Wikipedia origin. D13's citation is outside the deliberately narrow parenthetical syntax and produces no hint.

After these checks, exactly one authorized normal `Fetcher.fetch` invocation loaded the input-derived Metrology URL. The normal robots, DNS, response, encoding, and extraction behavior were retained. No search API, retry, or bypass was used. The intent journal, initially typed as an unknown outcome and marked non-retryable, was written and fsynced before loading. No matching cached full response was found in the two known DEV fetch-record directories.

The complete returned Fetcher response, including extracted text, links, source/requested URLs, timestamp, extraction metadata, and text hash, is saved privately. It was fetched at `2026-11-11T20:01:44.610535+00:00`, with text SHA-256 `9c889daf3404df5aa9555e89ef5687cc6e07cd914eb1f290a0d6cbbde3e91659`. This is the full Fetcher result, not an archive of raw HTTP bytes.

Matching was completed and saved before expected source labels or quotes were consulted:

| Input checked against the loaded page | Exact matches | Exact quotation coverage | Quoted evidence |
| --- | ---: | ---: | ---: |
| D14 | 1 | 15/15 words | Yes |
| Authored false citation to the same article | 0 | 0/10 words | None |

The D14 evidence is an identical source-text slice at offsets 5164–5303 and target slice 192–331. Afterwards, DEV labels confirmed the expected article origin and full literal quotation. The intentionally false citation still derives the same URL, but contributes no exact match; naming an article is kept separate from evidence that it supports the quotation.

Local checks also verified that a saved response is reused without another fetch, and that a pre-existing unknown-outcome journal prevents another fetch. Both tests mocked the fetch method to raise if called; the call count stayed zero. Saved source slices and text hashes were independently checked.

This is bounded positive evidence for direct named-reference loading on one real source, alongside one measured false-attribution negative. It does not establish general citation correctness, broad parser coverage, or an end-to-end queue improvement. The strict parser also does not cover nested parentheticals, arbitrary inflections, or citations outside its explicit syntax.

Implementation: [probe_named_reference_urls.py](../../scripts/probe_named_reference_urls.py). Private local checks, durable fetch intent, full response, matching trace, result, and retry-guard checks are under `data/private/web-evaluation/dev-named-reference-v1/`. No production files were edited.
