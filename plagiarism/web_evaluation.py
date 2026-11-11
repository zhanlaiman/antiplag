"""Offline scoring of saved web reports against an explicitly annotated manifest.

This module never searches, fetches pages, or calls models. A manifest is an
annotation artifact, not evidence that a representative benchmark exists.
"""

from collections import Counter
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from urllib.parse import quote, urlsplit, urlunsplit

from plagiarism.matching import literal_equivalent
from plagiarism.pdf_evidence import PrimarySupport


SCENARIOS = frozenset({"copy", "short_insertion", "multiple_sources", "quote", "paraphrase",
                       "translation", "independent", "template"})
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 32 * 1024 * 1024


class EvaluationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise EvaluationError(message)


def _identifier(value, label):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 200
             and all(ord(char) >= 32 and ord(char) != 127 for char in value), f"{label}: нужен непустой идентификатор без управляющих символов.")
    return value


def _hash(value, label):
    _require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None, f"{label}: нужен SHA-256 в нижнем регистре.")
    return value


def _span(value, label, length=None):
    _require(isinstance(value, list) and len(value) == 2 and all(type(item) is int for item in value)
             and 0 <= value[0] < value[1], f"{label}: нужен диапазон [start, end), start < end.")
    if length is not None:
        _require(value[1] <= length, f"{label}: диапазон выходит за пределы снимка текста.")
    return value


def _url(value):
    _require(isinstance(value, str) and len(value) <= 4096 and "\\" not in value
             and all(ord(char) >= 33 and ord(char) != 127 for char in value), "Некорректный URL источника.")
    try:
        parts = urlsplit(value)
        _require(parts.scheme in {"http", "https"} and bool(parts.hostname)
                 and parts.username is None and parts.password is None, "Нужен HTTP(S) URL без учётных данных.")
        host = parts.hostname.lower().rstrip(".")
        _require(bool(host), "Пустой hostname источника.")
        port = parts.port
        authority = "[" + host + "]" if ":" in host else host
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            authority += ":" + str(port)
        # Query parameters are meaningful; only fragments/default ports normalize.
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        path = re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group().upper(), path)
        query = re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group().upper(), query)
        return urlunsplit((parts.scheme, authority, path, query, ""))
    except ValueError as error:
        raise EvaluationError("Некорректный URL источника.") from error


def _read(path, maximum):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(maximum + 1)
        _require(len(raw) <= maximum, f"Файл слишком велик: {path}")
        return raw
    except OSError as error:
        raise EvaluationError(f"Не удалось прочитать файл: {path}") from error


def _json(path, maximum):
    raw = _read(path, maximum)

    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "Повторный JSON key.")
            value[key] = item
        return value

    def invalid_number(value):
        raise EvaluationError("JSON содержит неограниченное число: " + value)

    try:
        value = json.loads(raw, object_pairs_hook=unique_keys, parse_constant=invalid_number)
    except (ValueError, UnicodeError) as error:
        raise EvaluationError(f"Некорректный JSON: {path}") from error
    _require(isinstance(value, dict), f"Нужен JSON-объект: {path}")
    return value, raw


def _path(root, value, label):
    _require(isinstance(value, str) and bool(value.strip()), f"{label}: нужен путь к сохранённому файлу.")
    path = Path(value)
    return path if path.is_absolute() else root / path


def _report(report, case):
    """Reject wrong snapshots; quarantine ungrounded individual highlights."""
    _require(isinstance(report, dict), f"{case['id']}: нужен полный report snapshot.")
    text = report.get("text")
    _require(isinstance(text, str), f"{case['id']}: в report нет text.")
    try:
        digest = sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError as error:
        raise EvaluationError("report.text содержит некорректный Unicode.") from error
    _require(digest == case["text_sha256"], f"{case['id']}: SHA-256 report.text не совпадает с manifest.")
    _require(report.get("scope", "web_search") == "web_search", f"{case['id']}: это не web_search report.")
    _require(isinstance(report.get("sources"), list), f"{case['id']}: нужен закрытый полный снимок report.sources; публичного API payload недостаточно.")
    _require(isinstance(report.get("matches"), list) and isinstance(report.get("web"), dict), f"{case['id']}: отсутствуют matches/web.")
    sources = {}
    for source in report["sources"]:
        _require(isinstance(source, dict) and isinstance(source.get("text"), str), f"{case['id']}: некорректный source snapshot.")
        _require(bool(source["text"].strip()), f"{case['id']}: пустой source snapshot не подтверждает чтение.")
        identifier = _identifier(source.get("id"), "source.id")
        _require(identifier not in sources, f"{case['id']}: повторный source.id.")
        sources[identifier] = {**source, "canonical_url": _url(source.get("url"))}
    discovered = report["web"].get("discovered_urls")
    if discovered is not None:
        _require(isinstance(discovered, list), f"{case['id']}: discovered_urls должен быть массивом URL.")
        discovered = {_url(url) for url in discovered}
    errors = report["web"].get("errors", [])
    _require(isinstance(errors, list) and all(isinstance(item, dict) for item in errors), f"{case['id']}: некорректный web.errors.")
    valid, invalid, seen = [], [], set()
    primary_support = {}
    for index, match in enumerate(report["matches"]):
        try:
            _require(isinstance(match, dict) and match.get("kind") in {"exact", "similar"}, "Некорректная запись подсветки.")
            target = _span([match.get("start"), match.get("end")], "match.target", len(text))
            source = sources.get(match.get("source_id"))
            _require(source is not None, "У подсветки отсутствует source snapshot.")
            original = _span([match.get("source_start"), match.get("source_end")], "match.source", len(source["text"]))
            url = _url(match.get("source_url"))
            _require(url == source["canonical_url"], "URL подсветки не совпадает с source snapshot.")
            _require(match.get("excerpt") == text[target[0]:target[1]], "excerpt не подтверждается report.text.")
            _require(match.get("source_excerpt") == source["text"][original[0]:original[1]], "source_excerpt не подтверждается source.text.")
            if match["kind"] == "exact":
                _require(literal_equivalent(match["excerpt"], match["source_excerpt"]),
                         "Exact-подсветка содержит разные нормализованные тексты.")
            if source.get("representation_of"):
                primary_id = source["representation_of"]
                primary = sources.get(primary_id)
                _require(primary is not None and not primary.get("representation_of"),
                         "У дополнительного текста PDF отсутствует первичный снимок.")
                _require(primary["canonical_url"] == url, "Дополнительный текст ссылается на другой PDF.")
                if primary_id not in primary_support:
                    primary_support[primary_id] = PrimarySupport(primary["text"], primary_id)
                proof = primary_support[primary_id].evidence(match)
                _require(proof is not None and match.get("pdf_spacing_evidence") == proof,
                         "Дополнительная PDF-выдержка не подтверждена первичным текстом.")
            signature = (url, *target, *original)
            if match["kind"] == "exact" and signature not in seen:
                valid.append({"url": url, "target_span": target, "source_span": original})
                seen.add(signature)
        except (EvaluationError, TypeError) as error:
            invalid.append({"match_index": index, "error": str(error)})
    report_id = report.get("id")
    if report_id is not None:
        _identifier(report_id, "report.id")
    return {"text": text, "id": report_id, "sources": list(sources.values()), "discovered_urls": discovered,
            "matches": valid, "reported_exact_highlights": sum(isinstance(match, dict) and match.get("kind") == "exact" for match in report["matches"]),
            "invalid_highlights": invalid, "web": report["web"], "errors": errors}


def load_manifest(path, *, split="dev", allow_test=False, frozen_config_sha256=None):
    """Validate the whole manifest before selecting dev/test saved reports."""
    path = Path(path).resolve()
    manifest, raw = _json(path, MAX_MANIFEST_BYTES)
    _require(type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "Нужен manifest schema_version=1.")
    _require(isinstance(split, str) and split in {"dev", "test", "all"}, "split должен быть dev, test или all.")
    cases = manifest.get("cases")
    _require(isinstance(cases, list) and 0 < len(cases) <= 10000, "Нужен непустой массив cases (до 10000).")
    frozen = manifest.get("frozen_config_sha256")
    if frozen is not None:
        _hash(frozen, "frozen_config_sha256")
    identifiers, families, documents, hashes, annotations = set(), {}, {}, {}, set()
    source_splits, source_hash_splits = {}, {}
    for case in cases:
        _require(isinstance(case, dict), "Case должен быть объектом.")
        identifier = _identifier(case.get("id"), "case.id")
        _require(identifier not in identifiers, f"Повторный case.id: {identifier}")
        identifiers.add(identifier)
        document = _identifier(case.get("document_id"), f"{identifier}.document_id")
        family = _identifier(case.get("family_id"), f"{identifier}.family_id")
        case_split = case.get("split")
        _require(isinstance(case_split, str) and case_split in {"dev", "test"}, f"{identifier}: нужен split dev/test.")
        _require(isinstance(case.get("scenario"), str) and case["scenario"] in SCENARIOS, f"{identifier}: неизвестный scenario.")
        _require(type(case.get("synthetic")) is bool and type(case.get("reviewed")) is bool, f"{identifier}: synthetic/reviewed должны быть bool.")
        editorial = case.get("editorial_judgment", "not_evaluated")
        _require(isinstance(editorial, str) and editorial in {"not_evaluated", "needs_revision", "acceptable"},
                 f"{identifier}: неизвестный editorial_judgment.")
        digest = _hash(case.get("text_sha256"), f"{identifier}.text_sha256")
        _require(families.setdefault(family, case_split) == case_split, f"Family {family} попала в dev и test.")
        signature = (case_split, family, digest)
        _require(documents.setdefault(document, signature) == signature, f"Document {document} имеет разные split/family/text.")
        _require(hashes.setdefault(digest, document) == document, "Одинаковый текст записан как разные document_id.")
        _path(path.parent, case.get("report_path"), f"{identifier}.report_path")
        evidence = case.get("expected_evidence")
        _require(isinstance(evidence, list), f"{identifier}: нужен expected_evidence, включая [] для negative.")
        _require(not case["reviewed"] or bool(evidence) or case["scenario"] in {"independent", "template"},
                 f"{identifier}: reviewed positive case не содержит expected evidence.")
        evidence_seen = set()
        for gold in evidence:
            _require(isinstance(gold, dict), f"{identifier}: evidence должен быть объектом.")
            urls = gold.get("source_urls")
            _require(isinstance(urls, list) and bool(urls), f"{identifier}: нужен непустой source_urls.")
            canonical = [_url(url) for url in urls]
            _require(len(set(canonical)) == len(canonical), f"{identifier}: повторные URL-эквиваленты.")
            for url in canonical:
                _require(source_splits.setdefault(url, case_split) == case_split,
                         "Исходная публикация (gold source URL) попала в dev и test.")
            _span(gold.get("target_span"), f"{identifier}.target_span")
            _span(gold.get("source_span"), f"{identifier}.source_span")
            _require(type(gold.get("needs_lexical_highlight")) is bool, f"{identifier}: needs_lexical_highlight должен быть bool.")
            snapshot, source_hash = gold.get("source_snapshot_path"), gold.get("source_text_sha256")
            _require((snapshot is None) == (source_hash is None), f"{identifier}: source_snapshot_path и source_text_sha256 нужны вместе.")
            if snapshot is not None:
                _path(path.parent, snapshot, "source_snapshot_path")
                _hash(source_hash, "source_text_sha256")
                _require(source_hash_splits.setdefault(source_hash, case_split) == case_split,
                         "Одинаковый gold source snapshot попал в dev и test.")
            if "source_quote" in gold:
                _require(isinstance(gold["source_quote"], str), f"{identifier}: source_quote должен быть строкой.")
            key = (tuple(sorted(canonical)), tuple(gold["target_span"]), tuple(gold["source_span"]), gold["needs_lexical_highlight"])
            _require(key not in evidence_seen, f"{identifier}: повторная evidence annotation.")
            evidence_seen.add(key)
        annotation = (document, case["scenario"], tuple(sorted(evidence_seen)))
        _require(annotation not in annotations, f"{identifier}: повторный документ с той же разметкой.")
        annotations.add(annotation)
    selected = [case for case in cases if split == "all" or case["split"] == split]
    _require(bool(selected), "В выбранном split нет случаев.")
    if any(case["split"] == "test" for case in selected):
        _require(allow_test is True and frozen is not None and frozen_config_sha256 == frozen,
                 "Test требует --allow-test и --frozen-config-sha256, совпадающий с frozen manifest.")
    loaded, report_ids = [], {}
    for case in selected:
        report, report_raw = _json(_path(path.parent, case["report_path"], "report_path"), MAX_REPORT_BYTES)
        if "report" in report:
            report = report["report"]
        checked = _report(report, case)
        snapshot_hash = sha256(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if checked["id"] is not None:
            _require(report_ids.setdefault(checked["id"], snapshot_hash) == snapshot_hash,
                     "Один report.id обозначает разные сохранённые отчёты.")
        for gold in case["expected_evidence"]:
            _span(gold["target_span"], "gold.target_span", len(checked["text"]))
            if gold.get("source_snapshot_path") is not None:
                source_raw = _read(_path(path.parent, gold["source_snapshot_path"], "source_snapshot_path"), MAX_REPORT_BYTES)
                try:
                    source_text = source_raw.decode("utf-8")
                except UnicodeError as error:
                    raise EvaluationError("Gold source snapshot должен быть UTF-8 текстом.") from error
                _require(sha256(source_raw).hexdigest() == gold["source_text_sha256"], "SHA-256 gold source snapshot не совпадает.")
                start, end = _span(gold["source_span"], "gold.source_span", len(source_text))
                if "source_quote" in gold:
                    _require(source_text[start:end] == gold["source_quote"], "Gold source_quote не совпадает с source snapshot.")
        loaded.append({"case": case, "report": checked, "report_sha256": sha256(report_raw).hexdigest(),
                       "report_snapshot_sha256": snapshot_hash})
    return {"manifest_sha256": sha256(raw).hexdigest(), "frozen_config_sha256": frozen,
            "split": split, "manifest_case_count": len(cases), "cases": loaded}


def _overlap(span, expected):
    return max(0, min(span[1], expected[1]) - max(span[0], expected[0])) / (expected[1] - expected[0])


def _case_result(item):
    case, report = item["case"], item["report"]
    evidence = []
    read_urls = {source["canonical_url"] for source in report["sources"]}
    for gold in case["expected_evidence"]:
        urls = {_url(url) for url in gold["source_urls"]}
        read = bool(urls & read_urls)
        discovery_audit = None if report["discovered_urls"] is None else bool(urls & report["discovered_urls"])
        discovered = True if read else discovery_audit
        overlaps = [_overlap(match["target_span"], gold["target_span"]) for match in report["matches"] if match["url"] in urls]
        fraction = max(overlaps, default=0)
        highlighted = fraction >= 0.5
        if read and (not gold["needs_lexical_highlight"] or highlighted):
            outcome = "found"
        elif read:
            outcome = "span_missed"
        elif discovered is True:
            outcome = "source_unread"
        elif discovered is False:
            outcome = "source_not_discovered"
        else:
            outcome = "unknown"
        evidence.append({"source_urls": gold["source_urls"], "target_span": gold["target_span"],
                         "source_span": gold["source_span"], "needs_lexical_highlight": gold["needs_lexical_highlight"],
                         "source_snapshot_verified": gold.get("source_snapshot_path") is not None,
                         "discovered": discovered, "read": read, "highlighted": highlighted,
                         "discovery_observed_via_read": read and discovery_audit is not True,
                         "found_basis": "exact_target_highlight" if gold["needs_lexical_highlight"] else "source_read_only",
                         "target_overlap_fraction": round(fraction, 6), "outcome": outcome})
    technical = bool(report["errors"] or report["invalid_highlights"])
    coverage = ("complete" if report["web"].get("status") == "complete" and not technical
                and report["discovered_urls"] is not None and bool(report["sources"])
                else "partial" if report["web"].get("status") == "partial" else "unknown")
    if evidence:
        outcomes = {item["outcome"] for item in evidence}
        outcome = "found" if outcomes == {"found"} else "partial_found" if "found" in outcomes else next(iter(outcomes)) if len(outcomes) == 1 else "mixed_misses"
    else:
        outcome = "unexpected_exact_highlight" if report["reported_exact_highlights"] else "no_highlights_observed" if coverage == "complete" else "unknown"
    timing = report["web"].get("elapsed_seconds")
    attempts = report["web"].get("queries_attempted")
    return {key: case[key] for key in ("id", "document_id", "family_id", "split", "scenario", "synthetic", "reviewed")} | {
        "report_path": case["report_path"], "report_sha256": item["report_sha256"],
        "report_id": report["id"], "report_snapshot_sha256": item["report_snapshot_sha256"],
        "outcome": outcome, "coverage": coverage, "technical_errors": technical,
        "query_errors": [error for error in report["errors"] if error.get("stage") == "search"],
        "web_errors": report["errors"], "invalid_highlights": report["invalid_highlights"],
        "observed_exact_highlights": report["reported_exact_highlights"],
        "validated_exact_highlights": len(report["matches"]), "evidence": evidence,
        "elapsed_seconds": timing if type(timing) in (int, float) and math.isfinite(timing) and timing >= 0 else None,
        "queries_attempted": attempts if type(attempts) is int and attempts >= 0 else None,
        "editorial_judgment": case.get("editorial_judgment", "not_evaluated"),
        "editorial_interpretation": "quoted_overlap_is_not_an_accusation" if case["scenario"] == "quote" else "matching_evidence_only"}


def _metric(numerator, denominator, **extra):
    return {"numerator": numerator, "denominator": denominator, **extra}


def _metrics(cases):
    evidence = [gold for case in cases for gold in case["evidence"]]
    discovery = [gold for gold in evidence if gold["discovered"] is not None]
    highlights = [gold for gold in evidence if gold["needs_lexical_highlight"]]
    nonlexical = [gold for gold in evidence if not gold["needs_lexical_highlight"]]
    positives = [case for case in cases if case["evidence"]]
    negatives = {}
    for case in cases:
        if not case["evidence"]:
            negatives.setdefault(case["document_id"], []).append(case)
    return {"case_count": len(cases), "coverage_counts": dict(Counter(case["coverage"] for case in cases)),
            "technical_error_cases": sum(case["technical_errors"] for case in cases),
            "query_error_cases": sum(bool(case["query_errors"]) for case in cases),
            "source_discovery": _metric(sum(gold["discovered"] for gold in discovery), len(discovery), unknown=len(evidence) - len(discovery)),
            "source_reading": _metric(sum(gold["read"] for gold in evidence), len(evidence)),
            "lexical_highlight": _metric(sum(gold["highlighted"] for gold in highlights), len(highlights)),
            "lexical_evidence_found": _metric(sum(gold["outcome"] == "found" for gold in highlights), len(highlights)),
            "nonlexical_source_read": _metric(sum(gold["read"] for gold in nonlexical), len(nonlexical)),
            "evidence_found": _metric(sum(gold["outcome"] == "found" for gold in evidence), len(evidence),
                                      unknown=sum(gold["outcome"] == "unknown" for gold in evidence)),
            "all_expected_evidence_found": _metric(sum(case["outcome"] == "found" for case in positives), len(positives),
                unknown=sum(any(gold["outcome"] == "unknown" for gold in case["evidence"]) for case in positives)),
            "evidence_outcomes": dict(Counter(gold["outcome"] for gold in evidence)),
            "unexpected_exact_highlights": _metric(
                sum(any(case["observed_exact_highlights"] for case in rows) for rows in negatives.values()), len(negatives)),
            "negative_document_coverage_complete": sum(all(case["coverage"] == "complete" for case in rows) for rows in negatives.values()),
            "negative_document_coverage_unresolved": sum(any(case["coverage"] != "complete" for case in rows) for rows in negatives.values())}


def score(suite):
    cases = [_case_result(item) for item in suite["cases"]]
    reviewed = [case for case in cases if case["reviewed"]]
    reports = {}
    for case in cases:
        key = ("id", case["report_id"]) if case["report_id"] is not None else ("snapshot", case["report_snapshot_sha256"])
        reports.setdefault(key, case)
    timings = [case["elapsed_seconds"] for case in reports.values() if case["elapsed_seconds"] is not None]
    attempts = [case["queries_attempted"] for case in reports.values() if case["queries_attempted"] is not None]
    return {"schema_version": 1, "manifest_sha256": suite["manifest_sha256"],
            "frozen_config_sha256": suite["frozen_config_sha256"], "split": suite["split"],
            "summary": {"manifest_case_count": suite["manifest_case_count"], "selected_case_count": len(cases),
                "reviewed_case_count": len(reviewed), "excluded_unreviewed_case_count": len(cases) - len(reviewed),
                "synthetic_case_count": sum(case["synthetic"] for case in cases),
                "metrics_basis": "reviewed_annotations_only; synthetic results are reported separately",
                "metrics": _metrics(reviewed), "reviewed_real": _metrics([case for case in reviewed if not case["synthetic"]]),
                "reviewed_synthetic": _metrics([case for case in reviewed if case["synthetic"]]),
                "by_scenario": {scenario: _metrics([case for case in reviewed if case["scenario"] == scenario]) for scenario in sorted({case["scenario"] for case in cases})},
                "costs_unique_report_count": len(reports),
                "wall_seconds_observed_sum": sum(timings), "wall_seconds_reported_reports": len(timings),
                "wall_seconds_reported_cases": sum(case["elapsed_seconds"] is not None for case in cases),
                "query_attempts_observed_sum": sum(attempts), "query_attempts_reported_reports": len(attempts),
                "query_attempts_reported_cases": sum(case["queries_attempted"] is not None for case in cases)},
            "limitations": ["Only supplied URL equivalents and saved full snapshots are scored.",
                "expected_evidence annotates source/span similarity, not plagiarism; [] means no expected lexical matches in the annotation.",
                "reviewed is a manifest assertion, not a human review performed by this scorer.",
                "unexpected_exact_highlights counts reported exact highlights in documents with empty expected_evidence, once per document; invalid evidence stays flagged separately.",
                "source_discovery uses available audit/read observations; missing unread discovery audit remains unknown.",
                "Source URL/snapshot hashes are split-disjoint; related translations at different URLs still require human family grouping.",
                "Partial positive evidence can be found; partial negative reports do not establish absence of borrowing.",
                "Paraphrase/translation source reading is not a semantic verdict. Quotes are overlaps, not accusations.",
                "Observed costs are saved report timings/attempts, counted once per report ID or canonical snapshot, not per annotation case."], "cases": cases}


def score_manifest(path, **options):
    return score(load_manifest(path, **options))
