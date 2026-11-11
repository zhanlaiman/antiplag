#!/usr/bin/env python3
"""Evaluate an installed Ollama model and optionally compare a saved baseline."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plagiarism import evaluation, llm


def main():
    parser = argparse.ArgumentParser(description="Оценка локальной модели на контрольных примерах")
    parser.add_argument("--model", default=None, help="Имя установленной Ollama-модели")
    parser.add_argument("--split", choices=("train", "test", "all"), default="test")
    parser.add_argument("--output", type=Path, help="Сохранить полный результат в JSON")
    parser.add_argument("--baseline", type=Path, help="Сравнить с ранее сохранённым JSON")
    args = parser.parse_args()
    baseline = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        cases = [case for case in evaluation.examples() if args.split == "all" or case["split"] == args.split]
        baseline_ids = [item["example_id"] for item in baseline["results"]]
        if (not baseline.get("dataset_fingerprint")
                or baseline["dataset_fingerprint"] != evaluation.dataset_fingerprint(cases)
                or baseline_ids != [case["id"] for case in cases]):
            parser.exit(1, "Сравнение требует одинаковых текстов, источников и разметки с сохранённым отпечатком набора. Старый результат без отпечатка можно изучить отдельно.\n")
    ready = llm.status()
    name = llm.selected_model(args.model)
    if name not in ready["models"]:
        parser.exit(1, f"Модель {name} недоступна. Запустите Ollama и загрузите модель.\n")

    def progress(item, done, total):
        state = "ошибка запроса" if item.get("error") else "верно" if item["passed"] else "расхождение"
        print(f"[{done}/{total}] {item['example_id']}: {state}; ожидалось {item['expected']}, получено {item['predicted']}",
              file=sys.stderr, flush=True)

    result = evaluation.run(name, args.split, on_result=progress)
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.output)
    if baseline:
        print(f"Сравнение с {baseline['model']}")
        for key in ("accuracy", "precision", "recall", "recall_all_cases", "false_positive_rate", "coverage"):
            before, after = baseline["metrics"].get(key), result["metrics"].get(key)
            delta = round(after - before, 4) if before is not None and after is not None else None
            print(f"{key}: {before} → {after}; изменение {delta}")
    return 1 if result["metrics"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
