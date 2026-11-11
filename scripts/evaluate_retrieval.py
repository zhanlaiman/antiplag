#!/usr/bin/env python3
"""Compare word-based and hybrid retrieval without calling a generative model."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plagiarism import retrieval_evaluation


def main():
    parser = argparse.ArgumentParser(description="Оценка поиска источников на синтетических примерах")
    parser.add_argument("--split", choices=("train", "test", "all"), default="test")
    parser.add_argument("--mode", choices=("lexical", "hybrid", "both"), default="both")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--dataset", type=Path, default=retrieval_evaluation.DATASET_PATH)
    parser.add_argument("--output", type=Path, help="Сохранить полный результат в JSON")
    options = parser.parse_args()

    def progress(item, done, total):
        outcome = "ошибка запроса" if item.get("error") else (
            "тематические кандидаты для отдельной оценки" if item["expected_source_id"] is None
            else "источник найден" if item["source_hit"] else "источник пропущен")
        print(f"[{done}/{total}] {item['mode']} / {item['example_id']} — {outcome}", file=sys.stderr, flush=True)

    modes = ("lexical", "hybrid") if options.mode == "both" else (options.mode,)
    try:
        result = retrieval_evaluation.run(options.split, modes, options.top_k, options.dataset, on_result=progress)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"Не удалось провести оценку — {error}\n")
    print(json.dumps({"dataset": result["dataset"], "synthetic": True, "top_k": result["top_k"],
                      "metrics": result["metrics"], "interpretation": result["interpretation"]}, ensure_ascii=False, indent=2))
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = options.output.with_suffix(options.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(options.output)
    return int(any(metrics["errors"] for metrics in result["metrics"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
