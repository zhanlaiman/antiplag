"""Measure retrieval separately from pair classification on authored smoke cases."""

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import time


DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "retrieval_examples.json"
MODES = {"lexical", "hybrid"}
POSITIVE_KINDS = {"exact", "deep_paraphrase", "translation"}
MIN_SPAN_COVERAGE = 0.5


def load_dataset(path=DATASET_PATH):
    path = Path(path)
    raw = path.read_bytes()
    dataset = json.loads(raw)
    if not isinstance(dataset, dict) or not dataset.get("id") or dataset.get("synthetic") is not True:
        raise ValueError("Ожидается явно обозначенный синтетический набор поиска.")
    sources = dataset.get("sources", [])
    cases = dataset.get("cases", [])
    if not sources or not cases:
        raise ValueError("В наборе нужны источники и проверяемые примеры.")
    source_by_id = {}
    group_splits = {}
    for source in sources:
        source_id = source["id"]
        split = source["split"]
        if source_id in source_by_id or split not in {"train", "test"}:
            raise ValueError("Идентификаторы источников должны быть уникальны, разделы — train или test.")
        if not isinstance(source["text"], str) or not source["text"].strip():
            raise ValueError("У источника отсутствует текст.")
        group = source["group_id"]
        if group in group_splits and group_splits[group] != split:
            raise ValueError("Группа источника встречается одновременно в train и test.")
        group_splits[group] = split
        source_by_id[source_id] = source
    seen_ids = set()
    for case in cases:
        if case["id"] in seen_ids:
            raise ValueError("Идентификаторы примеров должны быть уникальны.")
        seen_ids.add(case["id"])
        if case["split"] not in {"train", "test"} or not isinstance(case["text"], str) or not case["text"].strip():
            raise ValueError("У примера некорректный раздел или пустой текст.")
        source_id = case.get("expected_source_id")
        if case["kind"] in POSITIVE_KINDS:
            source = source_by_id.get(source_id)
            if source is None or source["split"] != case["split"]:
                raise ValueError("Ожидаемый источник должен присутствовать в том же разделе набора.")
            excerpt = case.get("expected_source_excerpt")
            if not isinstance(excerpt, str) or not excerpt.strip() or source["text"].count(excerpt) != 1:
                raise ValueError("Ожидаемый фрагмент должен однозначно совпадать с исходным текстом.")
        elif case["kind"] != "independent_same_topic" or source_id is not None or case.get("expected_source_excerpt") is not None:
            raise ValueError("Независимые тексты должны иметь пустой ожидаемый источник.")
        for related_id in case.get("related_source_ids", []):
            source = source_by_id.get(related_id)
            if source is None or source["split"] != case["split"]:
                raise ValueError("Тематический источник должен присутствовать в том же разделе.")
    return dataset | {"fingerprint": sha256(raw).hexdigest()}


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _measure(case, candidates, source, top_k):
    selected = [item for item in candidates if 1 <= item["rank"] <= top_k]
    related_ids = set(case.get("related_source_ids", []))
    if source is None:
        return {
            "source_hit": None, "span_hit": None, "source_rank": None,
            "source_hit_at_1": None, "span_hit_at_1": None,
            "reciprocal_rank": None, "span_coverage": None,
            "related_source_retrieved": any(item["source_id"] in related_ids for item in selected),
        }
    excerpt = case["expected_source_excerpt"]
    expected_start = source["text"].index(excerpt)
    expected_end = expected_start + len(excerpt)
    relevant = [item for item in selected if item["source_id"] == source["id"]]
    rank = min((item["rank"] for item in relevant), default=None)
    coverage = max((max(0, min(item["source_end"], expected_end) - max(item["source_start"], expected_start))
                    / len(excerpt) for item in relevant), default=0)
    first_coverage = max((max(0, min(item["source_end"], expected_end) - max(item["source_start"], expected_start))
                          / len(excerpt) for item in relevant if item["rank"] == 1), default=0)
    return {
        "source_hit": bool(relevant), "span_hit": coverage >= MIN_SPAN_COVERAGE,
        "source_hit_at_1": rank == 1, "span_hit_at_1": first_coverage >= MIN_SPAN_COVERAGE,
        "source_rank": rank, "reciprocal_rank": 1 / rank if rank else 0,
        "span_coverage": round(coverage, 4), "related_source_retrieved": None,
    }


def _metrics(results):
    positive = [item for item in results if item["expected_source_id"] is not None]
    independent = [item for item in results if item["expected_source_id"] is None]
    errors = [item for item in results if item.get("error")]
    by_kind = {}
    for kind in sorted(POSITIVE_KINDS):
        cases = [item for item in positive if item["kind"] == kind]
        by_kind[kind] = {
            "cases": len(cases),
            "source_hit_at_1": _ratio(sum(bool(item.get("source_hit_at_1")) for item in cases), len(cases)),
            "span_hit_at_1": _ratio(sum(bool(item.get("span_hit_at_1")) for item in cases), len(cases)),
            "source_hit_at_k": _ratio(sum(bool(item.get("source_hit")) for item in cases), len(cases)),
            "span_hit_at_k": _ratio(sum(bool(item.get("span_hit")) for item in cases), len(cases)),
        }
    return {
        "total": len(results), "positive_cases": len(positive), "independent_cases": len(independent),
        "completed": len(results) - len(errors), "errors": len(errors),
        "source_hit_at_1": _ratio(sum(bool(item.get("source_hit_at_1")) for item in positive), len(positive)),
        "span_hit_at_1": _ratio(sum(bool(item.get("span_hit_at_1")) for item in positive), len(positive)),
        "source_hit_at_k": _ratio(sum(bool(item.get("source_hit")) for item in positive), len(positive)),
        "span_hit_at_k": _ratio(sum(bool(item.get("span_hit")) for item in positive), len(positive)),
        "source_mrr_at_k": _ratio(sum(item.get("reciprocal_rank") or 0 for item in positive), len(positive)),
        "independent_cases_with_candidates": sum(bool(item.get("candidates")) for item in independent),
        "independent_cases_with_related_source": sum(bool(item.get("related_source_retrieved")) for item in independent),
        "elapsed_seconds": round(sum(item["elapsed_seconds"] for item in results), 3),
        "by_kind": by_kind,
    }


def run(split="test", modes=("lexical", "hybrid"), top_k=5, dataset_path=DATASET_PATH,
        cache=None, on_result=None, retrieve_fn=None):
    """Compare retrieval modes using one split's library for each query.

    Top-k applies to each query chunk, matching the retrieval API. A span hit
    requires one returned passage to cover at least half the expected span.
    Failed requests remain in the positive denominator and are counted as errors.
    Independent same-topic cases do not have an expected plagiarism source and
    are excluded from hit/MRR denominators. Their returned candidates await a
    separate pair judgment, which this evaluator never performs.
    """
    if split not in {"train", "test", "all"}:
        raise ValueError("Выберите train, test или all.")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 10:
        raise ValueError("Можно запросить от 1 до 10 кандидатов на фрагмент.")
    modes = tuple(modes)
    if not modes or len(set(modes)) != len(modes) or set(modes) - MODES:
        raise ValueError("Выберите неповторяющиеся режимы lexical и hybrid.")
    dataset = load_dataset(dataset_path)
    if retrieve_fn is None:
        from plagiarism.retrieval import retrieve
        retrieve_fn = retrieve
    cases = [case for case in dataset["cases"] if split == "all" or case["split"] == split]
    if not cases:
        raise ValueError("В выбранном разделе нет примеров.")
    libraries = {name: [source for source in dataset["sources"] if source["split"] == name]
                 for name in ("train", "test")}
    source_by_id = {source["id"]: source for source in dataset["sources"]}
    results = []
    warnings = []
    for mode in modes:
        for case in cases:
            started = time.monotonic()
            item = {"example_id": case["id"], "split": case["split"], "kind": case["kind"],
                    "mode": mode, "expected_source_id": case.get("expected_source_id"),
                    "library_source_count": len(libraries[case["split"]]), "candidates": []}
            try:
                response = retrieve_fn(case["text"], libraries[case["split"]], mode=mode, top_k=top_k, cache=cache)
                item["candidates"] = response["candidates"]
                item["retrieval_stats"] = response.get("stats", {})
                item["embedding_model"] = response.get("embedding_model")
                item.update(_measure(case, response["candidates"], source_by_id.get(case.get("expected_source_id")), top_k))
                for warning in response.get("warnings", []):
                    if warning not in warnings:
                        warnings.append(warning)
            except (RuntimeError, ValueError, OSError) as error:
                item["error"] = str(error)
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            results.append(item)
            if on_result:
                on_result(item, len(results), len(cases) * len(modes))
    return {
        "dataset": dataset["id"], "dataset_fingerprint": dataset["fingerprint"],
        "synthetic": True, "usage": "development_smoke", "dataset_note": dataset["note"], "split": split,
        "top_k": top_k, "top_k_scope": "per_query_chunk", "min_span_coverage": MIN_SPAN_COVERAGE,
        "source_counts": {name: len(library) for name, library in libraries.items()},
        "case_counts": dict(Counter(case["kind"] for case in cases)),
        "metrics": {mode: _metrics([item for item in results if item["mode"] == mode]) for mode in modes},
        "results": results, "warnings": warnings,
        "interpretation": "Попадание источника и фрагмента оценивает поиск кандидатов. Независимые тексты исключены из знаменателя попаданий. Наличие тематического кандидата не является решением о заимствовании. Для оценки ложных подсветок нужен отдельный этап сравнения пар.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
