"""Development comparison of lexical matching with retrieval plus pair judging."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
import time

from plagiarism import llm, matching, retrieval, retrieval_evaluation


ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / ".runtime" / "pipeline-cache.json"
POSITIVE_RELATIONS = {"exact", "paraphrase"}


class JudgmentCache:
    """Atomically checkpoint successful pairs, retaining other processes' entries."""

    def __init__(self, path=CACHE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return {"version": 1, "entries": {}}
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("Файл контрольных ответов повреждён. Сохраните его копию перед повторным запуском.") from error
        if not isinstance(content, dict) or content.get("version") != 1 or not isinstance(content.get("entries"), dict):
            raise ValueError("Неизвестный формат контрольных ответов модели.")
        return content

    def get(self, key):
        with self.locked():
            return self.read()["entries"].get(key)

    def save(self, key, entry):
        with self.locked():
            content = self.read()
            content["entries"][key] = entry
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                 prefix=self.path.name + ".", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(content, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()


def model_digest(model):
    response = llm._request("/api/tags", timeout=3)
    for item in response.get("models", []):
        if item.get("name") in {model, model + ":latest"} and isinstance(item.get("digest"), str) and item["digest"]:
            return item["digest"]
    raise llm.OllamaError(f"Модель {model} недоступна или не сообщает контрольную сумму.")


def _key(text, source, provenance):
    payload = {**provenance, "text": text, "source": source}
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _grounded(judgment, text, source, query_start, source_start):
    if not isinstance(judgment, dict) or judgment.get("relation") not in llm.RELATIONS:
        raise llm.OllamaError("Ответ модели содержит неизвестный тип совпадения.")
    evidence = {}
    for field, original, offset in (("evidence_text", text, query_start), ("evidence_source", source, source_start)):
        excerpt = judgment.get(field)
        start, end = judgment.get(field + "_start"), judgment.get(field + "_end")
        if not isinstance(excerpt, str):
            raise llm.OllamaError("Ответ модели не содержит проверяемых выдержек.")
        if judgment["relation"] in POSITIVE_RELATIONS and len(excerpt.strip()) < 12:
            raise llm.OllamaError("Положительная находка не содержит достаточно подробной выдержки.")
        if excerpt:
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(original)
                    or original[start:end] != excerpt):
                raise llm.OllamaError("Позиции выдержки модели не совпадают с исходным текстом.")
            evidence[field] = excerpt
            evidence[field + "_start"] = offset + start
            evidence[field + "_end"] = offset + end
        else:
            if start is not None or end is not None:
                raise llm.OllamaError("Пустая выдержка имеет некорректные позиции.")
            evidence[field] = ""
            evidence[field + "_start"] = evidence[field + "_end"] = None
    return evidence


def _label(case, source_id):
    if source_id == case.get("expected_source_id"):
        return "expected_positive_source"
    if case["kind"] == "independent_same_topic" and source_id in case.get("related_source_ids", []):
        return "known_independent_source"
    return "unverified_source"


def _summary(results, engine):
    positive = [item for item in results if item["expected_source_id"] is not None]
    independent = [item for item in results if item["expected_source_id"] is None]
    found = [item for item in positive if item[engine]["source_hit"]]
    missed = [item["example_id"] for item in positive if not item[engine]["source_hit"]]
    false_pairs = sorted({(item["example_id"], pair["source_id"]) for item in results
                          for pair in item[engine]["pairs"] if pair["label"] == "known_independent_source" and pair.get("positive")})
    known_pairs = {(item["example_id"], pair["source_id"]) for item in results
                   for pair in item[engine]["pairs"] if pair["label"] == "known_independent_source" and not pair.get("error")}
    unverified = sorted({(item["example_id"], pair["source_id"]) for item in results
                         for pair in item[engine]["pairs"] if pair["label"] == "unverified_source" and pair.get("positive")})
    by_kind = {}
    for kind in sorted(retrieval_evaluation.POSITIVE_KINDS):
        cases = [item for item in positive if item["kind"] == kind]
        count = sum(item[engine]["source_hit"] for item in cases)
        by_kind[kind] = {"cases": len(cases), "sources_found": count,
                         "source_hit_rate": round(count / len(cases), 4) if cases else None}
    return {
        "positive_articles": len(positive), "positive_sources_found": len(found),
        "source_hit_rate": round(len(found) / len(positive), 4) if positive else None,
        "missed_expected_sources": missed, "independent_articles": len(independent),
        "independent_articles_with_positive_findings": sum(any(pair.get("positive") for pair in item[engine]["pairs"]) for item in independent),
        "independent_articles_fully_processed": sum(not item[engine].get("errors") for item in independent),
        "known_independent_source_pairs_evaluated": len(known_pairs),
        "false_positive_known_independent_source_pairs": [{"example_id": case_id, "source_id": source_id} for case_id, source_id in false_pairs],
        "unverified_positive_source_pairs": [{"example_id": case_id, "source_id": source_id} for case_id, source_id in unverified],
        "errors": sum(item[engine].get("errors", 0) for item in results),
        "uncertain_pairs": sum(pair.get("error_code") == "unconfirmed_exact" for item in results for pair in item[engine]["pairs"]),
        "request_errors": sum(item[engine].get("errors", 0) for item in results)
                          - sum(pair.get("error_code") == "unconfirmed_exact" for item in results for pair in item[engine]["pairs"]),
        "articles_with_errors": sum(bool(item[engine].get("errors")) for item in results),
        "elapsed_seconds": round(sum(item[engine]["elapsed_seconds"] for item in results), 3),
        "by_kind": by_kind,
    }


def audit_evidence_spans(result, dataset_path=retrieval_evaluation.DATASET_PATH):
    """Add an offset-only audit to a saved run without running search or Qwen.

    Touching an expected span checks evidence location. It cannot establish
    whether the model's explanation or its relation label is correct.
    """
    dataset = retrieval_evaluation.load_dataset(dataset_path)
    if result.get("dataset_fingerprint") != dataset["fingerprint"]:
        raise ValueError("Для аудита нужен тот же набор примеров, который использовался в прогоне.")
    audited = deepcopy(result)
    cases = {case["id"]: case for case in dataset["cases"]}
    sources = {source["id"]: source for source in dataset["sources"]}
    articles_with_evidence = 0
    positive_articles = 0
    wrong_passages = []
    for item in audited["results"]:
        if item["example_id"] not in cases:
            raise ValueError("Сохранённый пример отсутствует в исходном наборе.")
        case = cases[item["example_id"]]
        source_id = case.get("expected_source_id")
        hybrid = item["hybrid_qwen"]
        if source_id is None:
            hybrid["evidence_touches_expected_span"] = None
            continue
        positive_articles += 1
        source = sources[source_id]
        excerpt = case["expected_source_excerpt"]
        expected_start = source["text"].index(excerpt)
        expected_end = expected_start + len(excerpt)
        required_overlap = min(12, 0.25 * len(excerpt))
        article_touches = False
        for pair in hybrid["pairs"]:
            if pair["source_id"] != source_id or not pair.get("positive"):
                continue
            evidence = pair.get("evidence", {})
            start, end = evidence.get("evidence_source_start"), evidence.get("evidence_source_end")
            literal = (type(start) is int and type(end) is int
                       and 0 <= start < end <= len(source["text"])
                       and source["text"][start:end] == evidence.get("evidence_source"))
            overlap = max(0, min(end, expected_end) - max(start, expected_start)) if literal else 0
            touches = literal and overlap >= required_overlap
            span_audit = {
                "expected_source_start": expected_start, "expected_source_end": expected_end,
                "overlap_characters": overlap, "overlap_fraction": round(overlap / len(excerpt), 4),
                "required_overlap_characters": required_overlap,
                "literal_source_evidence_verified": literal,
                "evidence_touches_expected_span": touches,
            }
            pair["expected_span_audit"] = span_audit
            article_touches = article_touches or touches
            if not touches:
                wrong_passages.append({"example_id": item["example_id"], "source_id": source_id,
                    "candidate_id": pair["candidate"]["id"], "relation": pair.get("judgment", {}).get("relation"),
                    "overlap_fraction": span_audit["overlap_fraction"]})
        hybrid["evidence_touches_expected_span"] = article_touches
        articles_with_evidence += int(article_touches)
    metrics = audited["metrics"]["hybrid_qwen"]
    metrics["positive_articles_with_evidence_touching_expected_span"] = articles_with_evidence
    metrics["evidence_touches_expected_span_rate"] = round(articles_with_evidence / positive_articles, 4) if positive_articles else None
    metrics["wrong_source_passage_positive_findings"] = wrong_passages
    audited["evidence_span_audit"] = {
        "version": "expected-source-overlap-v1",
        "threshold_rule": "overlap_characters >= min(12, 0.25 * expected_excerpt_characters)",
        "note": "Пересечение выбранной выдержки с ожидаемым фрагментом подтверждает её расположение. Правильность смыслового вывода и метки exact или paraphrase требует отдельной оценки.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return audited


def run(split="test", top_k=5, on_result=None, *, model=None, max_cases=None,
        dataset_path=retrieval_evaluation.DATASET_PATH, cache_path=CACHE_PATH,
        retrieve_fn=None, judge_fn=None, analyze_fn=None, digest=None, adapter_hash=None,
        embedding_cache=None):
    """Run sequential pair comparisons with resumable, content-addressed answers.

    on_result(event, done, total) receives phase=pair progress during an article
    and phase=case when that article finishes. Source hit is a source-level
    signal. It does not establish that selected evidence explains the borrowing.
    """
    if split not in {"train", "test", "all"}:
        raise ValueError("Выберите train, test или all.")
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValueError("Можно запросить от 1 до 10 кандидатов на фрагмент.")
    if max_cases is not None and (type(max_cases) is not int or max_cases < 1):
        raise ValueError("Количество примеров должно быть положительным целым числом.")
    dataset = retrieval_evaluation.load_dataset(dataset_path)
    cases = [case for case in dataset["cases"] if split == "all" or case["split"] == split]
    if max_cases is not None:
        cases = cases[:max_cases]
    if not cases:
        raise ValueError("В выбранном разделе нет примеров.")
    name = llm.selected_model(model)
    provenance = {"model": name, "model_digest": digest or model_digest(name),
                  "adapter_sha256": adapter_hash or sha256(Path(llm.__file__).read_bytes()).hexdigest()}
    cache = JudgmentCache(cache_path)
    retrieve_fn, judge_fn, analyze_fn = retrieve_fn or retrieval.retrieve, judge_fn or llm.judge, analyze_fn or matching.analyze
    libraries = {name: [source for source in dataset["sources"] if source["split"] == name] for name in ("train", "test")}
    results = []
    started = time.monotonic()
    for case_index, case in enumerate(cases):
        library = libraries[case["split"]]
        source_by_id = {source["id"]: source for source in library}
        item = {"example_id": case["id"], "split": case["split"], "kind": case["kind"],
                "expected_source_id": case.get("expected_source_id"), "library_source_count": len(library)}
        lexical_started = time.monotonic()
        baseline = analyze_fn(case["text"], library)
        matched_ids = {match["source_id"] for match in baseline["matches"]}
        item["lexical"] = {
            "source_hit": case.get("expected_source_id") in matched_ids if case.get("expected_source_id") else None,
            "pairs": [{"source_id": source["id"], "label": _label(case, source["id"]), "positive": source["id"] in matched_ids} for source in library],
            "matches": baseline["matches"], "errors": 0, "elapsed_seconds": round(time.monotonic() - lexical_started, 3),
        }
        hybrid_started = time.monotonic()
        hybrid = {"source_hit": False if case.get("expected_source_id") else None, "pairs": [],
                  "errors": 0, "model_calls": 0, "cache_hits": 0}
        item["hybrid_qwen"] = hybrid
        try:
            response = retrieve_fn(case["text"], library, mode="hybrid", top_k=top_k, cache=embedding_cache)
            hybrid["retrieval_stats"] = response.get("stats", {})
            hybrid["embedding_model"] = response.get("embedding_model")
            candidates = [candidate for candidate in response["candidates"] if 1 <= candidate["rank"] <= top_k]
            hybrid["candidate_count"] = len(candidates)
            for pair_index, candidate in enumerate(candidates):
                pair_started = time.monotonic()
                source = source_by_id[candidate["source_id"]]
                pair = {"candidate": candidate, "source_id": source["id"], "label": _label(case, source["id"]), "positive": False}
                try:
                    text, source_text = candidate["query_excerpt"], candidate["source_excerpt"]
                    if (case["text"][candidate["query_start"]:candidate["query_end"]] != text
                            or source["text"][candidate["source_start"]:candidate["source_end"]] != source_text):
                        raise ValueError("Фрагмент поиска не совпадает с исходным текстом.")
                    key = _key(text, source_text, provenance)
                    cached = cache.get(key)
                    if cached is not None:
                        judgment = cached["judgment"]
                        pair["cached"] = True
                        pair["original_elapsed_seconds"] = cached["elapsed_seconds"]
                        hybrid["cache_hits"] += 1
                    else:
                        hybrid["model_calls"] += 1
                        judgment = judge_fn(text, source_text, name)
                        hybrid["model_calls"] += max(0, judgment.get("attempts", 1) - 1)
                        _grounded(judgment, text, source_text, candidate["query_start"], candidate["source_start"])
                        cache.save(key, {"judgment": judgment, **provenance,
                                         "elapsed_seconds": round(time.monotonic() - pair_started, 3),
                                         "created_at": datetime.now(timezone.utc).isoformat()})
                        pair["cached"] = False
                    pair["evidence"] = _grounded(judgment, text, source_text, candidate["query_start"], candidate["source_start"])
                    pair["literal_evidence_verified"] = True
                    pair["judgment"] = judgment
                    pair["positive"] = judgment["relation"] in POSITIVE_RELATIONS
                    if pair["positive"] and source["id"] == case.get("expected_source_id"):
                        hybrid["source_hit"] = True
                except (RuntimeError, ValueError, OSError) as error:
                    pair["error"] = str(error)
                    pair["error_code"] = getattr(error, "code", "model_error")
                    details = getattr(error, "details", None)
                    if details:
                        pair["error_details"] = details
                        hybrid["model_calls"] += max(0, details.get("attempts", 1) - 1)
                    hybrid["errors"] += 1
                pair["elapsed_seconds"] = round(time.monotonic() - pair_started, 3)
                hybrid["pairs"].append(pair)
                if on_result:
                    on_result({"phase": "pair", "example_id": case["id"], "pair_index": pair_index + 1,
                               "pair_total": len(candidates), "pair": pair}, case_index, len(cases))
        except (RuntimeError, ValueError, OSError) as error:
            hybrid["error"] = str(error)
            hybrid["errors"] += 1
        hybrid["elapsed_seconds"] = round(time.monotonic() - hybrid_started, 3)
        results.append(item)
        if on_result:
            on_result({"phase": "case", **item}, case_index + 1, len(cases))
    result = {
        "dataset": dataset["id"], "dataset_fingerprint": dataset["fingerprint"], "dataset_note": dataset["note"],
        "synthetic": True, "usage": "development_smoke", "split": split, "top_k": top_k,
        "top_k_scope": "per_query_chunk", "max_cases": max_cases, **provenance,
        "retrieval_sha256": sha256(Path(retrieval.__file__).read_bytes()).hexdigest(),
        "matching_sha256": sha256(Path(matching.__file__).read_bytes()).hexdigest(),
        "metrics": {engine: _summary(results, engine) for engine in ("lexical", "hybrid_qwen")},
        "results": results, "model_calls": sum(item["hybrid_qwen"]["model_calls"] for item in results),
        "cache_hits": sum(item["hybrid_qwen"]["cache_hits"] for item in results),
        "elapsed_seconds": round(time.monotonic() - started, 3), "created_at": datetime.now(timezone.utc).isoformat(),
        "interpretation": "Поиск и Qwen проверены на синтетических примерах разработки. Совпадение источника не подтверждает правильность смыслового вывода. Ложными парами считаются только положительные находки для явно размеченных независимых источников. Остальные положительные пары требуют разметки. Ошибки запросов учитываются отдельно; отсутствие находки при ошибке не подтверждает отсутствие заимствования.",
    }
    return audit_evidence_spans(result, dataset_path)
