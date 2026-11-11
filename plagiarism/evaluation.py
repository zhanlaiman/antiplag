"""Source-disjoint smoke evaluation for a local model; examples are synthetic."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from plagiarism import llm


EXAMPLES_PATH = Path(__file__).resolve().parent.parent / "data" / "examples.json"
POSITIVE_RELATIONS = {"exact", "paraphrase"}


def examples():
    with EXAMPLES_PATH.open(encoding="utf-8") as stream:
        return json.load(stream)


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def dataset_fingerprint(cases):
    content = [{key: case[key] for key in ("id", "source_id", "split", "expected", "text", "source_text")}
               for case in cases]
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run(model=None, split="test", on_result=None):
    if split not in ("train", "test", "all"):
        raise ValueError("Выберите train, test или all.")
    name = llm.selected_model(model)
    cases = [item for item in examples() if split == "all" or item["split"] == split]
    started = time.monotonic()
    results = []
    tp = fp = fn = tn = correct = errors = 0
    for case in cases:
        case_started = time.monotonic()
        item = {"example_id": case["id"], "source_id": case["source_id"],
                "expected": case["expected"], "predicted": None, "passed": False}
        try:
            judgment = llm.judge(case["text"], case["source_text"], name)
            predicted = judgment["relation"]
            item.update(predicted=predicted, judgment=judgment, passed=predicted == case["expected"])
            correct += int(item["passed"])
            expected_positive = case["expected"] in POSITIVE_RELATIONS
            predicted_positive = predicted in POSITIVE_RELATIONS
            tp += int(expected_positive and predicted_positive)
            fn += int(expected_positive and not predicted_positive)
            fp += int(not expected_positive and predicted_positive)
            tn += int(not expected_positive and not predicted_positive)
        except (llm.OllamaError, ValueError) as exc:
            errors += 1
            item["error"] = str(exc)
        item["elapsed_seconds"] = round(time.monotonic() - case_started, 3)
        results.append(item)
        if on_result:
            on_result(item, len(results), len(cases))
    metrics = {
        "total": len(cases), "completed": len(cases) - errors, "correct": correct, "errors": errors,
        "accuracy": _ratio(correct, len(cases)),
        "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
        "false_positive_rate": _ratio(fp, fp + tn), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "coverage": _ratio(len(cases) - errors, len(cases)),
        "recall_all_cases": _ratio(tp, sum(case["expected"] in POSITIVE_RELATIONS for case in cases)),
    }
    return {"model": name, "split": split, "metrics": metrics, "results": results,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "dataset_fingerprint": dataset_fingerprint(cases), "task": "pair_classification",
            "judge_version": llm.JUDGE_VERSION,
            "metrics_note": "Точность по четырём классам учитывает ошибки запросов как неверный ответ. Precision, recall и частота ложных срабатываний рассчитаны по завершённым ответам; coverage показывает долю завершённых проверок. recall_all_cases учитывает все примеры заимствований, включая ошибки запросов. Эта проверка оценивает сравнение готовой пары, а не поиск источника.",
            "dataset": "synthetic-smoke-v1", "dataset_note": "Авторские синтетические примеры для отладки. Их результаты использованы при настройке промпта. Для независимой оценки качества нужна новая ручная разметка реальных текстов."}
