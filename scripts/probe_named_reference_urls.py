#!/usr/bin/env python3
"""Bounded explicit named-reference experiment; no query or production changes.

Local checks read only D01–D30. A separate explicit action can invoke the normal
Fetcher exactly once for the URL derived from D14 input. The prewritten unknown
outcome journal prevents retries after interruption. Hints are never evidence.
"""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.matching import _tokens, analyze, literal_equivalent

DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
OUT = ROOT / "data/private/web-evaluation/dev-named-reference-v1"
QUOTE = r'«([^«»]+)»|“([^“”]+)”|„([^„“]+)“|"([^"\n]+)"'
TITLE = re.compile(r'\b(?:статья|статье|article)\s*[:—–-]?\s*(?:' + QUOTE + ')', re.I)
WIKI = re.compile(r'\b(?:Википедия|Russian\s+Wikipedia)\b', re.I)
NEGATION = re.compile(r'\b(?:не|not)\b', re.I)
FALSE_SENTENCE = "Зелёный термометр по вторникам выдаёт квадратные булочки вместо температуры воздуха"
FALSE_CITATION = '«' + FALSE_SENTENCE + '» (русская Википедия, статья «Метрология»).'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def group_span(match):
    return match.span(next(i for i in range(1, 5) if match.group(i) is not None))


def derive_urls(text):
    """Submitted text only; return unverified hints, never source claims."""
    hints, rejected = [], []
    for citation in re.finditer(r'\(([^()\r\n]{1,300})\)', text):
        content = citation.group(1)
        title, wiki = TITLE.search(content), WIKI.search(content)
        if title is None or wiki is None:
            continue
        start, end = group_span(title)
        without_title = content[:start] + " " * (end - start) + content[end:]
        if NEGATION.search(without_title):
            rejected.append({"citation_span": list(citation.span()), "reason": "explicit_negation_in_citation"})
            continue
        raw = content[start:end]
        normalized = " ".join(raw.split())
        if (not normalized or any(unicodedata.category(c).startswith("C") for c in normalized)
                or normalized in {".", ".."}):
            rejected.append({"citation_span": list(citation.span()), "reason": "invalid_title"})
            continue
        url = "https://ru.wikipedia.org/wiki/" + quote(normalized.replace(" ", "_"), safe="")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "ru.wikipedia.org" or not parsed.path.startswith("/wiki/") or parsed.query or parsed.fragment:
            raise ValueError("Derived title escaped the fixed Wikipedia origin")
        offset = citation.start(1)
        attached = None
        for passage in re.finditer(QUOTE, text[:citation.start()]):
            if not text[passage.end():citation.start()].strip():
                attached = list(group_span(passage))
        hint = {"url": url, "status": "unverified_input_reference", "raw_title": raw,
                "normalized_title": normalized, "title_span": [offset + start, offset + end],
                "citation_span": list(citation.span()), "source_marker_span": [offset + wiki.start(), offset + wiki.end()],
                "attached_quoted_passage_span": attached,
                "origin_provenance": "Fixed ru.wikipedia.org origin plus safely quoted title explicitly present in submitted text; no labels, URLs, or source text used."}
        if not any(item["url"] == url for item in hints):
            hints.append(hint)
    return {"hints": hints, "rejected_explicit_references": rejected}


def dev_texts():
    cases = json.loads((DEV / "key.json").read_text())["cases"]
    if [c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)] or any(c["split"] != "dev" for c in cases):
        raise ValueError("Only the fixed D01–D30 DEV inputs are permitted")
    result = {}
    for case in cases:
        path = (DEV / case["input_path"]).resolve()
        if not path.is_relative_to(DEV.resolve()):
            raise ValueError("Input path leaves DEV")
        text = path.read_text()
        if digest(text) != case["text_sha256"]:
            raise ValueError("Changed DEV input")
        result[case["id"]] = text
    return result


def authored_checks():
    passage = '«Точные измерения помогают проверить качество работы школьного датчика температуры» '
    rows = [
        ("other_article", passage + '(русская Википедия, статья «Шахматные часы»).', 1, "Шахматные_часы"),
        ("absent_source_marker", passage + '(учебник, статья «Метрология»).', 0, None),
        ("absent_explicit_article", passage + '(русская Википедия, «Метрология»).', 0, None),
        ("explicit_negation", passage + '(не русская Википедия, статья «Метрология»).', 0, None),
        ("negated_article", passage + '(Википедия, не статья «Метрология»).', 0, None),
        ("english_marker", passage + '(Russian Wikipedia, article “Metrology”).', 1, "Metrology"),
        ("false_attribution", FALSE_CITATION, 1, "Метрология"),
        ("path_injection_stays_fixed_origin", passage + '(Википедия, статья «https://example.test/a?x=1#f»).', 1, "https://example.test/a?x=1#f"),
    ]
    checked = []
    for identifier, text, count, path in rows:
        result = derive_urls(text)
        if len(result["hints"]) != count:
            raise ValueError("Authored parser check failed: " + identifier)
        if count and unquote(result["hints"][0]["url"]).split("/wiki/", 1)[1] != path:
            raise ValueError("Authored title serialization failed: " + identifier)
        checked.append({"id": identifier, "text": text, "expected_hints": count, **result})
    return checked


def cache_candidates(url):
    """Inspect only known DEV full-fetch records, never other evaluation splits."""
    directories = (ROOT / "data/private/web-evaluation/dev-live-30-v1",
                   ROOT / "data/private/web-evaluation/dev-exact-search-pilot")
    examined, matches = [], []
    for directory in directories:
        for path in sorted(directory.glob("*-fetch.json")):
            examined.append(str(path.relative_to(ROOT)))
            record = json.loads(path.read_text())
            fetched = record.get("fetched", record)
            if (isinstance(fetched, dict) and fetched.get("requested_url", fetched.get("url")) == url
                    and isinstance(fetched.get("text"), str) and fetched.get("fetched_at")
                    and fetched.get("text_sha256") == digest(fetched["text"])):
                matches.append((path, fetched))
    return examined, matches


def local_checks():
    OUT.mkdir(mode=0o700, exist_ok=True)
    os.chmod(OUT, 0o700)
    texts = dev_texts()
    rows = [{"id": cid, "text_sha256": digest(text), **derive_urls(text)} for cid, text in texts.items()]
    d14 = next(row for row in rows if row["id"] == "D14")
    if len(d14["hints"]) != 1:
        raise ValueError("D14 must independently derive exactly one explicit named-reference URL")
    examined, cached = cache_candidates(d14["hints"][0]["url"])
    result = {"schema_version": 1, "generated_at": now(), "planner_input": "Submitted input text only",
              "production_changed": False, "query_strategy_changed": False, "live_search_queries": 0,
              "fetch_invocations": 0, "correct_url_found": None, "confirmed_highlight": None,
              "documents": len(rows), "documents_with_hints": sum(bool(row["hints"]) for row in rows),
              "cases": rows, "authored_checks": authored_checks(),
              "cache_full_fetch_records_examined": examined,
              "matching_cached_full_fetch_records": [str(path.relative_to(ROOT)) for path, _ in cached]}
    dump(OUT / "local-hints.json", result)
    return texts, result


def fetch_once(text, hint):
    intent_path, response_path = OUT / "D14-fetch-intent.json", OUT / "D14-fetch-response.json"
    if response_path.exists():
        record = json.loads(response_path.read_text())
        if record["requested_url"] != hint["url"] or digest(record["response"]["text"]) != record["response"]["text_sha256"]:
            raise ValueError("Saved response identity changed")
        return record
    if intent_path.exists():
        return {"state": "failed_or_unknown_no_retry", "prior_intent": json.loads(intent_path.read_text()),
                "code": "fetch_outcome_not_repeated"}
    examined, cached = cache_candidates(hint["url"])
    intent = {"schema_version": 1, "state": "attempted_outcome_unknown", "code": "fetch_outcome_unknown",
              "created_at": now(), "requested_url": hint["url"], "input_text_sha256": digest(text),
              "input_hint": hint, "max_fresh_fetch_invocations": 1,
              "retry_allowed": False, "cache_records_examined": examined}
    with intent_path.open("x", encoding="utf-8") as output:
        os.chmod(intent_path, 0o600)
        json.dump(intent, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        if cached:
            path, response = cached[0]
            provenance, invocations = "cached_normal_fetch_response:" + str(path.relative_to(ROOT)), 0
        else:
            from plagiarism.web_fetch import Fetcher
            response = Fetcher().fetch(hint["url"])
            provenance, invocations = "one_authorized_normal_Fetcher.fetch_invocation", 1
        if response["text_sha256"] != digest(response["text"]):
            raise ValueError("Fetched text hash does not match the full saved response")
        result = {"schema_version": 1, "state": "complete", "completed_at": now(),
                  "requested_url": hint["url"], "input_text_sha256": digest(text),
                  "fetch_invocations": invocations, "provenance": provenance, "response": response}
        dump(response_path, result)
        dump(intent_path, {**intent, "state": "complete", "code": None, "completed_at": result["completed_at"],
                           "fetch_invocations": invocations, "response_path": response_path.name,
                           "response_text_sha256": response["text_sha256"]})
        return result
    except Exception as error:
        result = {**intent, "state": "failed", "code": getattr(error, "code", type(error).__name__),
                  "details": getattr(error, "details", None), "completed_at": now(), "fetch_invocations": 0 if cached else 1}
        dump(intent_path, result)
        return result


def match_result(text, source, passage_span):
    result = analyze(text, [{"id": "input-derived-reference", "url": source["url"],
                             "title": source["title"], "text": source["text"]}])
    exact = [match for match in result["matches"] if match["kind"] == "exact"]
    tokens = _tokens(text)
    selected = [token for token in tokens if passage_span and passage_span[0] <= token.start and token.end <= passage_span[1]]
    covered = [token for token in selected if any(match["start"] <= token.start and token.end <= match["end"] for match in exact)]
    return {"analysis": result, "exact_matches": len(exact), "quote_words": len(selected),
            "exact_covered_quote_words": len(covered), "quoted_exact_matches": sum(match["quoted"] for match in exact)}


def evaluate_response(text, hint, fetched):
    if fetched.get("state") != "complete":
        result = {"schema_version": 1, "state": "no_fetched_source", "fetch_outcome": fetched,
                  "correct_url_found": None, "confirmed_highlight": None,
                  "conclusion": "Typed fetch failure or unknown outcome retained; no retry or source-truth claim."}
        dump(OUT / "result.json", result)
        return result
    source = fetched["response"]
    positive = match_result(text, source, hint["attached_quoted_passage_span"])
    negative_hint = derive_urls(FALSE_CITATION)["hints"][0]
    negative = match_result(FALSE_CITATION, source, negative_hint["attached_quoted_passage_span"])
    # Freeze all matching before consulting expected source labels/quotes.
    dump(OUT / "D14-matching-before-gold.json", {"generated_at": now(), "input_text_sha256": digest(text),
         "source_text_sha256": source["text_sha256"], "positive": positive,
         "false_citation": {"text": FALSE_CITATION, "hint": negative_hint, **negative}})
    case = next(case for case in json.loads((DEV / "key.json").read_text())["cases"] if case["id"] == "D14")
    exact = [match for match in positive["analysis"]["matches"] if match["kind"] == "exact"]
    canonical = lambda url: unquote(url).rstrip("/")
    gold = []
    for evidence in case["expected_evidence"]:
        relevant = [match for match in exact if evidence["target_span"][0] <= match["start"]
                    and match["end"] <= evidence["target_span"][1]]
        gold.append({"source_id": evidence["source_id"], "expected_origin_url_confirmed_after_matching":
                     any(canonical(source["url"]) == canonical(url) for url in evidence["source_urls"]),
                     "full_expected_literal_quote_confirmed": any(literal_equivalent(evidence["target_quote"], match["source_excerpt"]) for match in relevant),
                     "evidence_marked_as_quoted": any(match["quoted"] for match in relevant)})
    prior_path = ROOT / "data/private/web-evaluation/dev-live-30-v1/D14.json"
    prior = json.loads(prior_path.read_text())
    if digest(prior["text"]) != digest(text):
        raise ValueError("Prior saved DEV report input changed")
    prior_exact = [match for match in prior["matches"] if match["kind"] == "exact"]
    quote_tokens = [token for token in _tokens(text) if hint["attached_quoted_passage_span"][0] <= token.start
                    and token.end <= hint["attached_quoted_passage_span"][1]]
    prior_covered = sum(any(match["start"] <= token.start and token.end <= match["end"] for match in prior_exact)
                        for token in quote_tokens)
    result = {"schema_version": 1, "state": "complete", "generated_at": now(),
              "live_search_queries": 0, "fetch_invocations": fetched["fetch_invocations"],
              "source_url": source["url"], "source_requested_url": source["requested_url"],
              "source_fetched_at": source["fetched_at"], "source_text_sha256": source["text_sha256"],
              "source_origin_provenance": hint, "normal_fetch_provenance": fetched["provenance"],
              "positive": {key: value for key, value in positive.items() if key != "analysis"},
              "false_citation": {key: value for key, value in negative.items() if key != "analysis"},
              "gold_used_only_after_matching": gold,
              "prior_saved_report_comparison": {"path": str(prior_path.relative_to(ROOT)),
                  "report_id": prior["id"], "previous_sources_checked": len(prior["sources"]),
                  "derived_source_previously_loaded": any(canonical(item["url"]) == canonical(source["url"]) for item in prior["sources"]),
                  "exact_covered_quote_words_before": prior_covered,
                  "exact_covered_quote_words_with_derived_source": positive["exact_covered_quote_words"],
                  "scope": "Saved earlier report versus this separate source load; the production queue was not rerun."},
              "confirmed_highlight": positive["exact_covered_quote_words"] > 0,
              "false_citation_supplies_no_exact_match": negative["exact_matches"] == 0,
              "integration_or_queue_selection_tested": False,
              "conclusion": "One bounded input-derived source load and independent lexical check; a citation alone remains an unverified hint."}
    dump(OUT / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-d14-once", action="store_true")
    args = parser.parse_args()
    texts, local = local_checks()
    hint = next(row for row in local["cases"] if row["id"] == "D14")["hints"][0]
    if not args.fetch_d14_once:
        print(json.dumps({"documents": local["documents"], "documents_with_hints": local["documents_with_hints"],
                          "authored_checks": len(local["authored_checks"]), "D14_input_derived_hint": hint,
                          "cached_full_responses": local["matching_cached_full_fetch_records"]}, ensure_ascii=False, indent=2))
        return
    print(json.dumps(evaluate_response(texts["D14"], hint, fetch_once(texts["D14"], hint)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
