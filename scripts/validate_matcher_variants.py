#!/usr/bin/env python3
"""Offline matcher audit on DEV-30 and labelled artificial perturbations.

Never reads a sealed/final collection; source URL is used only for scoring.
"""
import argparse
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import matching as candidate
from plagiarism.web_evaluation import _url


def baseline(ref):
    code = subprocess.check_output(["git", "show", ref + ":plagiarism/matching.py"], cwd=ROOT, text=True)
    module = types.ModuleType("matcher_audit_baseline")
    sys.modules[module.__name__] = module
    exec(compile(code, "git:" + ref, "exec"), module.__dict__)
    return module


def tails_only():
    code = (ROOT / "plagiarism/matching.py").read_text()
    begin = code.index("    selected = []\n    for candidate in matches:", code.index("def _similar_matches"))
    end = code.index("\n\n\ndef _deduplicate", begin)
    code = code[:begin] + "    return matches" + code[end:]
    module = types.ModuleType("matcher_audit_tails_only")
    sys.modules[module.__name__] = module
    exec(compile(code, "candidate:tails-only", "exec"), module.__dict__)
    return module


def coverage(module, target, sources):
    result = module.analyze(target, sources)
    tokens = module._tokens(target)
    exact = [m for m in result["matches"] if m["kind"] == "exact"]
    return {"matches": result["matches"], "exact_token_indices": [i for i, token in enumerate(tokens)
            if any(m["start"] <= token.start and token.end <= m["end"] for m in exact)],
            "invalid_exact": [m for m in exact if not getattr(module, "literal_equivalent",
                lambda a, b: bool(module.normalize(a)) and module.normalize(a) == module.normalize(b))
                (m["excerpt"], m["source_excerpt"])]}


def dev_audit(old, new, dev, reports):
    cases = json.loads((dev / "key.json").read_text())["cases"]
    assert [c["id"] for c in cases] == [f"D{i:02}" for i in range(1, 31)]
    assert all(c["split"] == "dev" for c in cases)
    rows = []
    for case in cases:
        report = json.loads((reports / (case["id"] + ".json")).read_text())
        target = report["text"]
        tokens = new._tokens(target)
        before, after = (coverage(mod, target, report["sources"]) for mod in (old, new))
        evidence = []
        for gold in case["expected_evidence"]:
            if not gold["needs_lexical_highlight"]:
                continue
            a, b = gold["target_span"]
            wanted = [i for i, t in enumerate(tokens) if a <= t.start and t.end <= b]
            alignment = SequenceMatcher(None, [tokens[i].value for i in wanted],
                                        [t.value for t in new._tokens(gold["source_quote"])], autojunk=False)
            copied = {wanted[i] for block in alignment.get_matching_blocks()
                      for i in range(block.a, block.a + block.size)}
            expected_urls = {_url(url) for url in gold["source_urls"]}
            counts = []
            for result in (before, after):
                exact = [m for m in result["matches"] if m["kind"] == "exact" and _url(m["source_url"]) in expected_urls]
                gold_covered = {i for i in copied if any(m["start"] <= tokens[i].start and tokens[i].end <= m["end"] for m in exact)}
                counts.append({"any_source_words": len(copied & set(result["exact_token_indices"])),
                               "expected_source_words": len(gold_covered)})
            evidence.append({"expected_copied_words": len(copied), "before": counts[0], "after": counts[1]})
        removed = [m for m in before["matches"] if not any(all(m.get(k) == n.get(k) for k in
                    ("kind", "source_id", "start", "end", "source_start", "source_end")) for n in after["matches"])]
        added = [m for m in after["matches"] if not any(all(m.get(k) == n.get(k) for k in
                    ("kind", "source_id", "start", "end", "source_start", "source_end")) for n in before["matches"])]
        rows.append({"id": case["id"], "scenario": case["scenario"], "evidence": evidence,
                     "before_exact_words": len(before["exact_token_indices"]), "after_exact_words": len(after["exact_token_indices"]),
                     "invalid_exact_after": after["invalid_exact"], "removed": removed, "added": added})
    return rows, cases


def source(text, label="artificial"):
    return {"id": label, "title": label, "url": "https://example.invalid/" + label, "text": text}


def artificial_pairs(cases, dev):
    pairs = []
    for case in cases:
        for number, gold in enumerate(case["expected_evidence"]):
            if not gold["needs_lexical_highlight"]:
                continue
            quote = gold["source_quote"]
            tokens = candidate._tokens(quote)
            if len(tokens) < 6:
                continue
            label = case["id"] + "-" + str(number)
            variants = {"unaltered": quote, "nbsp": quote.replace(" ", "\u00a0"),
                        "quotation_marks": "«" + quote.replace("«", '"').replace("»", '"') + "»"}
            middle = len(tokens) // 2
            cut = tokens[middle].start
            variants["insert_three_words"] = quote[:cut] + "добавлены новые уточняющие " + quote[cut:]
            # Split a substantive word; the source is held constant and target changes.
            long = next((t for t in tokens if len(t.value) >= 8), None)
            if long:
                split = long.start + max(3, (long.end - long.start) // 2)
                variants["line_break_hyphen"] = quote[:split] + "-\n" + quote[split:]
            for kind, target in variants.items():
                pairs.append({"id": label + "-" + kind, "kind": kind, "synthetic": True,
                              "target": target, "source": quote, "positive": True})
            # Same sentences in reverse order; both sentences are separately available.
            full = (dev / gold["source_snapshot_path"]).read_text()
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", full)
                         if 10 <= len(candidate._tokens(s)) <= 40]
            if len(sentences) >= 2:
                pairs.append({"id": label + "-sentence_permutation", "kind": "sentence_permutation", "synthetic": True,
                              "target": sentences[1] + " " + sentences[0], "source": sentences[0] + " " + sentences[1],
                              "positive": True})
    negatives = [
        ("Фотограф поставил камеру у окна и проверил освещение комнаты перед съёмкой портрета.",
         "Для цифровой фотографии важно заранее выбрать фокусное расстояние и правильно оценить яркость фона."),
        ("Шахматист быстро нажал кнопку часов после сильного хода и задумался над планом соперника.",
         "Контроль времени в шахматной партии ограничивает продолжительность размышлений каждого игрока."),
        ("Учитель нарисовал прямоугольный треугольник на доске, чтобы класс повторил формулы площади.",
         "Доказательство геометрической теоремы требует последовательного перехода от известных условий к заключению."),
        ("Экономист сравнил цены на продукты в двух городах за последний месяц.",
         "Спрос меняется под воздействием доходов населения и доступности заменяющих товаров."),
        ("Архивист аккуратно разложил письма по годам и сфотографировал повреждённые страницы.",
         "Исторические документы сохраняют в специальных условиях, чтобы бумага медленнее разрушалась."),
        ("Программист проверил порядок чисел перед запуском поиска в отсортированном массиве.",
         "Двоичный поиск сокращает диапазон возможных ответов за счёт сравнения с центральным элементом."),
    ]
    pairs.extend({"id": "independent-topic-" + str(i), "kind": "independent_same_topic", "synthetic": True,
                  "target": a, "source": b, "positive": False} for i, (a, b) in enumerate(negatives, 1))
    # Tight counterexamples for anchored-tail logic and fuzzy attribution suppression.
    seed = "Редактор проверил каждую строку перед публикацией заметки"
    tail = "и сохранил исходные материалы"
    for gap in ("ещё", "теперь", "совсем"):
        pairs.append({"id": "anchored-tail-" + gap, "kind": "anchored_one_word", "synthetic": True,
                      "target": seed + " " + gap + " " + tail, "source": seed + " " + tail, "positive": True})
    for i, gap in enumerate((". Ещё ", "\n\nЕщё ", " лишние два ")):
        pairs.append({"id": "anchor-boundary-" + str(i), "kind": "anchor_boundary_control", "synthetic": True,
                      "target": seed + gap + tail, "source": seed + " " + tail, "positive": True,
                      "tail_must_not_be_exact": True})
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="a5a9aac")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dev = ROOT / "data/private/web-evaluation/materials/dev-30"
    reports = ROOT / "data/private/web-evaluation/dev-live-30-v1"
    old = baseline(args.baseline)
    rows, cases = dev_audit(old, candidate, dev, reports)
    pairs = artificial_pairs(cases, dev)
    for pair in pairs:
        for name, module in (("before", old), ("after", candidate)):
            result = coverage(module, pair["target"], [source(pair["source"])])
            result["exact_words"] = len(result.pop("exact_token_indices"))
            result["target_words"] = len(module._tokens(pair["target"]))
            source_tokens = module._tokens(pair["source"])
            result["source_words"] = len(source_tokens)
            result["exact_source_words"] = sum(any(m["kind"] == "exact" and m["source_start"] <= token.start
                and token.end <= m["source_end"] for m in result["matches"]) for token in source_tokens)
            pair[name] = result
    steps = []
    if old.MATCHER_VERSION == "lexical-v2-kind-aware-dedup" and candidate.MATCHER_VERSION == "lexical-v3-anchored-tails":
        tails = tails_only()
        tails_rows, _ = dev_audit(old, tails, dev, reports)
        trim_rows, _ = dev_audit(tails, candidate, dev, reports)
        steps = [{"change": "anchored_tails_and_preserve_fuzzy_coverage", "cases": tails_rows},
                 {"change": "suppress_source_already_exact_attribution_expansion", "cases": trim_rows}]
    result = {"baseline": old.MATCHER_VERSION, "candidate": candidate.MATCHER_VERSION,
              "network_calls": 0, "dev_cases": rows, "artificial_pairs": pairs,
              "isolated_steps": steps,
              "metrics_note": "Source-faithful and any-source copied-word coverage are distinct. Artificial figures are not real-world accuracy."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    changed = [r["id"] for r in rows if r["removed"] or r["added"]]
    print(json.dumps({"dev_cases": len(rows), "changed": changed,
          "artificial_pairs": len(pairs), "invalid_exact_after": sum(bool(r["invalid_exact_after"]) for r in rows),
          "negative_pairs": sum(not p["positive"] for p in pairs),
          "negative_highlights_after": sum(bool(p["after"]["matches"]) for p in pairs if not p["positive"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
