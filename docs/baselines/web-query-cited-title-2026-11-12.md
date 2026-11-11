# Cited-title query hypothesis, 2026-11-12

Do not promote this candidate. It changes only D14 on DEV, has no measured retrieval benefit, and loses the only whole-copy query in an independent authored counterexample. Production planning and its default strategy remain unchanged.

The input-only planner requires a quotation of at least ten words immediately followed by a parenthetical citation, up to 300 characters, with explicit `статья`, `статье`, or `article` followed by a quoted title. It preserves the quotation's first ten-word query and replaces at most one later query wholly inside the same quotation whose token positions overlap that anchor by at least 80% of the secondary query. The replacement quotes the first six words and appends title tokens as unquoted context. The exact actual query count is preserved; six remains the maximum. The title comes from submitted text, never expected evidence, a URL, or a source snapshot.

The bounded local run covered all 30 DEV inputs, the existing 348-document/420-insertion long matrix, and the existing 162-document short matrix. Both artificial matrices have text-hash sequences identical to the saved earlier matrices. Seven explicit-pattern checks passed. Plans were frozen before evidence scoring. No network/provider/fetch calls occurred, and URL/highlight outcomes remain null.

All fragment-level results below are unchanged between each baseline and its candidate. The numerator for the middle column requires the complete query to lie in the expected target/insertion and occur in its source span; the last column requires only six consecutive shared words from the insertion. These are local query clues, not retrieval results.

| Collection and baseline | Actual queries, baseline → candidate | Whole query in target and source | At least six shared words |
| --- | ---: | ---: | ---: |
| DEV, citation-v2 | 46 → 46 | 10/18 lexical fragments | 12/18 |
| DEV, dense-v4 | 167 → 167 | 14/18 lexical fragments | 17/18 |
| Short matrix, citation-v2 | 162 → 162 | 62/162 insertions | 70/162 |
| Short matrix, dense-v4 | 766 → 766 | 132/162 insertions | 142/162 |
| Long matrix, either baseline | 2088 → 2088 | 185/420 insertions | 190/420 |

Only D14 supplies the explicit title pattern. Citation-v2 has no secondary query and stays unchanged. Dense-v4 replaces its second query with `"прослеживаемость чаще всего достигается путем калибровки" метрология`. Its first ten-word quotation query is retained. The replacement's six-word phrase lies inside the copy and shares six source words, but its appended title lies outside the copy and the complete query is not a literal source-span phrase. D14 therefore has two literal whole-copy query slots before replacement and one afterwards, although the retained first slot preserves its fragment-level hit. Across DEV, dense query containment inside target spans remains 15/18; requiring source-span literalness yields the separate 14/18 above.

The authored loss begins a long quotation with the two author words `И вот`, followed by a ten-word copy, and ends with an explicit article title. The first ten-word query contains two author words plus eight copied words. Its overlapping secondary query consists of the entire ten-word copy and qualifies at exactly 80% overlap. Replacing it with the first six words plus title removes the only query wholly contained in the copy and source. Actual requests remain 2 → 2. The best shared run drops 10 → 8 because the first query survives; the replacement itself shares only four copied words. Thus the six-shared-word metric stays positive while whole-copy containment is lost. The planner was not retuned around this loss.

The normal artificial matrices contain no qualifying cited titles, so their unchanged results do not demonstrate safety for mixed quotations. A cited title may be a useful discovery clue, but there is no fresh retrieval evidence here, and positional overlap alone does not establish that a secondary query is redundant.

Implementation: [probe_cited_title_queries.py](../../scripts/probe_cited_title_queries.py). Private plans, actual query counts, discontiguous phrase/title spans, sent-token origins, source-shared scores, transitions, and the complete authored loss are saved under `data/private/web-evaluation/query-cited-title-v1/`. The main matrix was executed once; the requested authored counterexample was added afterwards without rerunning those matrices. The script refuses to overwrite a frozen run.
