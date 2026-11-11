#!/usr/bin/env python3
"""Compare saved local pipeline runs without making any model requests."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics


SHARED_FIELDS = ("dataset_fingerprint", "adapter_sha256", "retrieval_sha256", "matching_sha256", "split", "top_k")
CANDIDATE_FIELDS = ("id", "query_index", "rank", "query_start", "query_end", "query_excerpt",
                    "source_id", "source_start", "source_end", "source_excerpt")
EXACT_INPUT_FIELDS = tuple(field for field in CANDIDATE_FIELDS if field not in ("id", "rank"))


def _inputs(run):
    return [{"metadata": {key: value for key, value in case.items()
                          if key not in ("lexical", "hybrid_qwen")},
             "candidates": [{field: pair["candidate"][field] for field in CANDIDATE_FIELDS}
                            for pair in case["hybrid_qwen"]["pairs"]]}
            for case in run["results"]]


def _aligned_order_changes(first_inputs, second_inputs):
    if len(first_inputs) != len(second_inputs):
        raise ValueError("Состав исходных статей различается.")
    reordered_cases = []
    for first_case, second_case in zip(first_inputs, second_inputs):
        if first_case["metadata"] != second_case["metadata"]:
            raise ValueError("Исходные статьи или их эталонные метаданные различаются.")
        first_pairs = [tuple(candidate[field] for field in EXACT_INPUT_FIELDS)
                       for candidate in first_case["candidates"]]
        second_pairs = [tuple(candidate[field] for field in EXACT_INPUT_FIELDS)
                        for candidate in second_case["candidates"]]
        if Counter(first_pairs) != Counter(second_pairs):
            raise ValueError("Точные входы кандидатов, их границы или число повторений различаются.")
        if first_pairs != second_pairs:
            reordered_cases.append(first_case["metadata"]["example_id"])
    return reordered_cases


def _latency(pairs):
    values = sorted(float(pair["elapsed_seconds"]) for pair in pairs if not pair.get("cached"))
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("В сохранённом прогоне указано некорректное время проверки.")
    return {"uncached_pairs": len(values), "cached_pairs": sum(bool(pair.get("cached")) for pair in pairs),
            "median_seconds": round(statistics.median(values), 3) if values else None,
            "p95_seconds": round(values[math.ceil(0.95 * len(values)) - 1], 3) if values else None}


def _summary(run):
    pairs = [pair for case in run["results"] for pair in case["hybrid_qwen"]["pairs"]]
    uncertain = sum(pair.get("error_code") == "unconfirmed_exact" for pair in pairs)
    other_pair_errors = sum(bool(pair.get("error")) and bool(pair.get("error_code"))
                            and pair["error_code"] != "unconfirmed_exact" for pair in pairs)
    unclassified_pair_errors = sum(bool(pair.get("error")) and not pair.get("error_code") for pair in pairs)
    retrieval_errors = sum(bool(case["hybrid_qwen"].get("error")) for case in run["results"])
    return {"model": run["model"], "model_digest": run["model_digest"],
            "metrics": run["metrics"]["hybrid_qwen"],
            "uncertain_pairs": uncertain, "other_pair_errors": other_pair_errors,
            "unclassified_pair_errors": unclassified_pair_errors,
            "retrieval_errors": retrieval_errors, "model_requests": run["model_calls"],
            "total_elapsed_seconds": run["elapsed_seconds"], "pair_latency": _latency(pairs)}


def compare_runs(first, second, *, align_identical_inputs=False):
    for run in (first, second):
        for field in ("model_digest", "dataset_fingerprint", "adapter_sha256", "retrieval_sha256", "matching_sha256"):
            if not isinstance(run.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", run[field]):
                raise ValueError(f"В сохранённом прогоне отсутствует корректный SHA256 поля {field}.")
    for field in SHARED_FIELDS:
        if first.get(field) != second.get(field):
            raise ValueError(f"Прогоны различаются по полю {field}. Для сравнения нужны одинаковые данные и код.")
    first_inputs, second_inputs = _inputs(first), _inputs(second)
    if not first_inputs:
        raise ValueError("В сохранённых прогонах нет статей для сравнения.")
    reordered_cases = []
    if align_identical_inputs:
        reordered_cases = _aligned_order_changes(first_inputs, second_inputs)
    elif first_inputs != second_inputs:
        raise ValueError("Исходные статьи, порядок кандидатов или их точные фрагменты различаются.")
    pair_count = sum(len(case["candidates"]) for case in first_inputs)
    if not pair_count:
        raise ValueError("В сохранённых прогонах нет пар для сравнения.")
    return {"comparable": True, "dataset_fingerprint": first["dataset_fingerprint"],
            "comparison_mode": "align_identical_inputs" if align_identical_inputs else "strict",
            "candidate_order_changed_cases": reordered_cases,
            "cases": len(first_inputs), "candidate_pairs": pair_count,
            "first": _summary(first), "second": _summary(second),
            "latency_note": "Время зависит от оборудования, порядка запросов, прогрева, кеша и состояния памяти. Изменение порядка кандидатов отмечено отдельно. Эти прогоны не позволяют отделить влияние каждого фактора на задержку.",
            "interpretation": "Сопоставлены сохранённые ответы на одинаковые пары с одинаковым кодом. Набор синтетический и использован для отладки. Результаты не оценивают качество на новых реальных статьях. Время медианы и p95 включает повторные попытки и ошибки, исключая ответы из кеша. Параметры моделей и состояние памяти следует учитывать отдельно."}


def main():
    parser = argparse.ArgumentParser(description="Сравнить два сохранённых прогона без запуска моделей")
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--align-identical-inputs", action="store_true",
                        help="Сверить мультимножества точных входов внутри каждой статьи, игнорируя только id и rank кандидатов")
    args = parser.parse_args()
    try:
        first = json.loads(args.first.read_text(encoding="utf-8"))
        second = json.loads(args.second.read_text(encoding="utf-8"))
        result = compare_runs(first, second, align_identical_inputs=args.align_identical_inputs)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Не удалось сравнить прогоны — {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
