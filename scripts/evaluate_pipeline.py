#!/usr/bin/env python3
"""Run a resumable local retrieval-and-Qwen development diagnostic."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plagiarism import pipeline_evaluation


def main():
    parser = argparse.ArgumentParser(description="Сравнить поиск по словам с поиском по смыслу и проверкой Qwen")
    parser.add_argument("--split", choices=("train", "test", "all"), default="test")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-cases", type=int, help="Ограничить число примеров для диагностики")
    parser.add_argument("--dataset", type=Path, default=pipeline_evaluation.retrieval_evaluation.DATASET_PATH)
    parser.add_argument("--cache", type=Path, default=pipeline_evaluation.CACHE_PATH)
    parser.add_argument("--output", type=Path, help="Сохранить полный результат в JSON")
    args = parser.parse_args()

    def progress(event, done, total):
        if event["phase"] == "pair":
            pair = event["pair"]
            status = pair.get("error") or pair["judgment"]["relation"]
            cached = " · сохранённый ответ" if pair.get("cached") else ""
            print(f"[{done + 1}/{total}] {event['example_id']} · пара {event['pair_index']}/{event['pair_total']} · {status}{cached}", file=sys.stderr, flush=True)
        else:
            hybrid = event["hybrid_qwen"]
            print(f"Завершено {done}/{total} · {event['example_id']} · ошибок {hybrid['errors']}", file=sys.stderr, flush=True)

    try:
        result = pipeline_evaluation.run(args.split, args.top_k, progress, model=args.model,
                                         max_cases=args.max_cases, dataset_path=args.dataset, cache_path=args.cache)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"Не удалось провести оценку — {error}\n")
    print(json.dumps({"dataset": result["dataset"], "synthetic": True, "metrics": result["metrics"],
                      "model_calls": result["model_calls"], "cache_hits": result["cache_hits"],
                      "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)
    return int(any(metrics["errors"] for metrics in result["metrics"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
