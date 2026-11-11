#!/usr/bin/env python3
"""Compare blind query plans and separately test matching on frozen DEV snapshots.

The experiment has no provider or fetcher. It cannot return discovery or fetch metrics.
Run from a checkout containing the private dev-30 evaluation materials.
"""

import argparse
from bisect import bisect_left
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plagiarism.matching import MATCHER_VERSION, MIN_WORDS, _tokens, analyze
from plagiarism.web_search import DISTINCTIVE_STRATEGY, MAX_QUERIES, MAX_URLS, QUERY_STRATEGY, plan_queries


DEV = ROOT / "data/private/web-evaluation/materials/dev-30"
STRATEGIES = (QUERY_STRATEGY, DISTINCTIVE_STRATEGY)


def digest(text):
    return sha256(text.encode()).hexdigest()


def read_checked(base, entry, path_field, expected_hash):
    path = (base / entry[path_field]).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Путь документа выходит за пределы каталога настройки.")
    text = path.read_bytes().decode("utf-8")
    if digest(text) != expected_hash:
        raise ValueError(f"Изменился снимок {path.name}")
    return text


def shared_run(left, right):
    """Longest contiguous sequence in both token lists (left is a short query)."""
    previous, best = [0] * (len(left) + 1), 0
    for word in right:
        current = [0]
        for index, check in enumerate(left):
            current.append(previous[index] + 1 if check == word else 0)
        best = max(best, max(current))
        previous = current
    return best


def clip_tokens(tokens, span):
    return [t for t in tokens if span[0] <= t.start and t.end <= span[1]]


def probe_evidence(queries, tokens, expected, source_words):
    target_span = expected["target_span"]
    best_overlap = best_shared = 0
    whole = False
    for q in queries:
        overlap = clip_tokens(tokens, (max(target_span[0], q["start"]), min(target_span[1], q["end"])))
        # Match what was actually sent including provider-safe truncation of long words.
        query_words = q["query"][1:-1].split()
        aligned = clip_tokens(tokens, (q["start"], q["end"]))
        query_in_target = [word for word, token in zip(query_words, aligned)
                           if target_span[0] <= token.start and token.end <= target_span[1]]
        if not overlap:
            continue
        run = shared_run(query_in_target, source_words)
        best_overlap, best_shared = max(best_overlap, len(overlap)), max(best_shared, run)
        whole |= run == len(query_words)
    return {"position_words": best_overlap, "shared_consecutive_words": best_shared,
            "whole_query_in_both_spans": whole}


def conditional_highlight(matches, target_tokens, source_tokens, evidence):
    """Count exact matching token pairs only when BOTH ends fall in the specified spans."""
    target_starts = [t.start for t in target_tokens]
    source_starts = [t.start for t in source_tokens]
    covered = set()
    target_span, source_span = evidence["target_span"], evidence["source_span"]
    for item in matches:
        if item["kind"] != "exact" or item["source_id"] != evidence["source_id"]:
            continue
        a = bisect_left(target_starts, item["start"])
        b = bisect_left(source_starts, item["source_start"])
        for left, right in zip(target_tokens[a:], source_tokens[b:]):
            if left.end > item["end"] or right.end > item["source_end"]:
                break
            if (target_span[0] <= left.start and left.end <= target_span[1]
                    and source_span[0] <= right.start and right.end <= source_span[1]):
                covered.add(left.start)
    total = len(clip_tokens(target_tokens, target_span))
    return {"covered_words": len(covered), "expected_words": total,
            "half_or_more": total > 0 and len(covered) * 2 >= total}


def make_long_probe(pages):
    """Reconstruct the documented 710/40 geometry, separately from the 30 cases."""
    # Two published texts. The host never belongs to this one-source conditional test.
    host = pages["wikisource-ru-1039896-5244621"]["text"]
    source = pages["wiki-ru-168721-150358889"]
    host_tokens, source_tokens = _tokens(host), _tokens(source["text"])
    prefix = host[host_tokens[80].start:host_tokens[399].end]
    suffix = host[host_tokens[400].start:host_tokens[749].end]
    start, end = source_tokens[32].start, source_tokens[71].end
    excerpt = source["text"][start:end]
    text = prefix + "\n\n" + excerpt + "\n\n" + suffix
    offset = len(prefix) + 2
    evidence = {"source_id": source["id"], "source_span": [start, end],
                "target_span": [offset, offset + len(excerpt)], "needs_lexical_highlight": True,
                "source_quote": excerpt, "target_quote": excerpt}
    if len(_tokens(text)) != 710 or len(_tokens(excerpt)) != 40:
        raise ValueError("Изменилась геометрия контрольной вставки.")
    case = {"id": "R710", "scenario": "insertion_40_of_710", "synthetic": True,
            "text_sha256": digest(text), "expected_evidence": [evidence]}
    return case, text, [source]


def diagnose(case, text, pages, source_tokens):
    # Generate each plan using the text alone, before reading evidence or sources.
    plans, planning_ms = {}, {}
    for strategy in STRATEGIES:
        started = perf_counter()
        plans[strategy] = plan_queries(text, strategy=strategy)
        planning_ms[strategy] = round((perf_counter() - started) * 1000, 3)
    started = perf_counter()
    real_matches = analyze(text, pages)["matches"]
    matching_ms = round((perf_counter() - started) * 1000, 3)
    by_source = {p["id"]: p for p in pages}
    target_tokens = _tokens(text)
    checked = []
    for gold in case["expected_evidence"]:
        s = by_source[gold["source_id"]]["text"]
        if (text[slice(*gold["target_span"])] != gold["target_quote"]
                or s[slice(*gold["source_span"])] != gold["source_quote"]):
            raise ValueError(f"Не совпадают контрольные выдержки {case['id']}.")
        words = source_tokens[gold["source_id"]]
        expected_words = [t.value for t in clip_tokens(words, gold["source_span"])]
        checked.append({
            "source_id": gold["source_id"], "source_span": gold["source_span"],
            "target_span": gold["target_span"], "lexical_expected": gold["needs_lexical_highlight"],
            "plans": {strategy: probe_evidence(queries, target_tokens, gold, expected_words)
                      for strategy, queries in plans.items()},
            "conditional_exact": conditional_highlight(real_matches, target_tokens, words, gold)})
    counts = Counter(m["kind"] for m in real_matches)
    if any(len(q) > MAX_QUERIES for q in plans.values()) or len(pages) > MAX_URLS:
        raise ValueError("Превышен бюджет диагностики.")
    return {"id": case["id"], "scenario": case["scenario"], "synthetic": case["synthetic"],
            "text_sha256": case["text_sha256"], "word_count": len(target_tokens),
            "query_spans": {k: [[q["start"], q["end"]] for q in v] for k, v in plans.items()},
            "planned_queries": {k: len(v) for k, v in plans.items()}, "plan_ms": planning_ms,
            "offline_pages": list(by_source), "matching_ms": matching_ms,
            "exact_highlights": counts["exact"], "similar_highlights": counts["similar"],
            "evidence": checked, "provider_found_url": None, "site_fetched_source": None,
            "live_queries": 0, "live_fetches": 0}


def measure(base):
    key_path = base / "key.json"
    cases = json.loads(key_path.read_text())["cases"]
    if ([c["id"] for c in cases] != [f"D{i:02d}" for i in range(1, 31)]
            or any(c["split"] != "dev" for c in cases)):
        raise ValueError("Ожидаются только 30 настроечных документов D01–D30.")
    texts = {c["id"]: read_checked(base, c, "input_path", c["text_sha256"]) for c in cases}
    pages = {}
    for source in json.loads((base / "sources.json").read_text()):
        text = read_checked(base, source, "path", source["provenance"]["text_sha256"])
        pages[source["id"]] = {**source, "text": text}
    origin_only = {c["published_target"]["source_id"] for c in cases if c.get("published_target")}
    common = sorted((p for p in pages.values() if p["id"] not in origin_only), key=lambda p: p["id"])
    if len(common) != MAX_URLS:
        raise ValueError("Состав фиксированной библиотеки для подсветки изменился.")
    tokens = {p["id"]: _tokens(p["text"]) for p in common}
    results = []
    for case in cases:
        selected = common
        if case["id"] in ("D16", "D17"):
            peer = "D17" if case["id"] == "D16" else "D16"
            # Same event, same fixed limit. The other 11 snapshots are unchanged.
            selected = common[:-1] + [{"id": peer, "title": peer, "text": texts[peer]}]
        results.append(diagnose(case, texts[case["id"]], selected, tokens))
    probe, text, selected = make_long_probe(pages)
    reconstruction = diagnose(probe, text, selected, tokens)
    return {"schema_version": 1, "dev_key_sha256": sha256(key_path.read_bytes()).hexdigest(),
            "source_text_sha256": {s["id"]: s["provenance"]["text_sha256"] for s in pages.values()},
            "matcher": MATCHER_VERSION, "production_strategy": QUERY_STRATEGY, "strategies": STRATEGIES,
            "query_cap": MAX_QUERIES, "fetch_cap": MAX_URLS, "gold_reviewed": sum(c["reviewed"] for c in cases),
            "cases": results, "reconstruction": reconstruction}, text


def render_table(bundle):
    rows = bundle["cases"]
    evidence = [e for row in rows for e in row["evidence"]]
    literal = [e for e in evidence if e["lexical_expected"]]
    negatives = [r for r in rows if not r["evidence"]]
    strategies = bundle["strategies"]

    def total_pairs(items, field, threshold):
        return " → ".join(str(sum(e["plans"][s][field] >= threshold for e in items)) for s in strategies)

    def pair_for_case(row, field):
        if not row["evidence"]:
            return "—"
        return " → ".join(" + ".join(str(int(e["plans"][s][field])) for e in row["evidence"])
                          for s in strategies)

    def metric_lines(row):
        planned = " → ".join(str(row["planned_queries"][s]) for s in strategies)
        cover = " + ".join(f"{m['covered_words']}/{m['expected_words']}"
                           for m in [e["conditional_exact"] for e in row["evidence"]])
        if not cover:
            cover = "—"
        return (f"| {row['id']} | {row['scenario']} | {planned} | {pair_for_case(row, 'position_words')} | "
                f"{pair_for_case(row, 'shared_consecutive_words')} | {pair_for_case(row, 'whole_query_in_both_spans')} | "
                f"{cover} | {row['exact_highlights']} / {row['similar_highlights']} |")

    def hits(row, s):
        return sum(e["plans"][s]["shared_consecutive_words"] >= MIN_WORDS for e in row["evidence"])

    wins = [r["id"] for r in rows if hits(r, strategies[1]) > hits(r, strategies[0])]
    losses = [r["id"] for r in rows if hits(r, strategies[1]) < hits(r, strategies[0])]
    planned = " → ".join(str(sum(r["planned_queries"][s] for r in rows)) for s in strategies)
    latency = " → ".join(f"{sum(r['plan_ms'][s] for r in rows):.0f}" for s in strategies)
    return "\n".join([
        "# Выбор поисковых фраз на 30 настроечных документах",
        "",
        f"Слева равномерные окна `{strategies[0]}`, справа `{strategies[1]}`. "
        "У каждого способа тот же предел в шесть фраз и двенадцать страниц на документ. "
        "Вариант ранжирует окна по редкости сочетаний внутри переданного текста. Сначала он выбирает разные абзацы, "
        "затем допускает дополнительные неперекрывающиеся окна. Поисковые фразы строятся только по входному тексту.",
        "",
        f"Размечено {len(evidence)} участков в {len(rows)} документах, из них {len(literal)} допускают дословную подсветку. "
        f"Документов с разметкой, подтверждённой человеком, {bundle['gold_reviewed']}. "
        "Закрытые материалы исключены. Эти числа относятся только к диагностике на данном наборе.",
        "",
        "| Наблюдение | Равномерные → характерные |",
        "| --- | --- |",
        f"| Запланировано фраз | {planned} |",
        f"| Фраза включает хотя бы одно слово из заданного участка, все {len(evidence)} участков | {total_pairs(evidence, 'position_words', 1)} |",
        f"| Внутри заданных границ совпали подряд хотя бы шесть слов, дословные участки | {total_pairs(literal, 'shared_consecutive_words', MIN_WORDS)} из {len(literal)} |",
        f"| В обеих выдержках встретилась целая поисковая фраза, дословные участки | {total_pairs(literal, 'whole_query_in_both_spans', 1)} из {len(literal)} |",
        "| Живые поисковые запросы и сетевые загрузки | 0 → 0 |",
        "| Возврат правильного URL и загрузка сайта в живом поиске | Нет измерения |",
        f"| Время построения всех планов на этом устройстве, мс | {latency} |",
        "",
        f"При одинаковых доступных источниках дословная подсветка покрыла хотя бы половину слов "
        f"в {sum(e['conditional_exact']['half_or_more'] for e in literal)} из {len(literal)} участков, "
        f"каждое слово в {sum(e['conditional_exact']['expected_words'] == e['conditional_exact']['covered_words'] for e in literal)}. "
        f"На {len(negatives)} отрицательных документах появилось {sum(r['exact_highlights'] for r in negatives)} "
        f"дословных и {sum(r['similar_highlights'] for r in negatives)} приблизительных подсветок. "
        f"Общая сверка заняла {sum(r['matching_ms'] for r in rows) / 1000:.2f} с и выполнялась один раз на документ, "
        "поскольку выбор фраз в этой проверке не меняет страницы. Это время локального сопоставления.",
        "",
        f"По числу участков с шестью общими словами улучшились {', '.join(wins) or '—'}. "
        f"Ухудшились {', '.join(losses) or '—'}. В рабочем сайте по умолчанию остаются равномерные окна.",
        "",
        "## По каждому документу",
        "",
        "Позиция показывает максимум слов поискового окна внутри намеченного участка. Общие слова — "
        "самая длинная дословная последовательность внутри размеченных участков с обеих сторон. "
        "Целая фраза равна 1, только если весь отправляемый текст запроса встретился в обеих выдержках. "
        "В последнем столбце показаны все находки движка во всех доступных снимках. Знак «+» разделяет "
        "два отдельных участка. Прочерк означает, что в документе нет ожидаемого заимствования.",
        "",
        "| Документ | Тип | Фразы | Позиция, слов | Общие слова подряд | Целая фраза | Подсвечено / слов участка | Найдено дословных / приблизительных |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        *(metric_lines(row) for row in rows),
        "",
        "## Контрольная реконструкция",
        "",
        "Отдельный искусственный текст R710 содержит 670 слов Писарева и 40 слов из статьи «Метрология». "
        "Вставка занимает слова 321–360 по счёту с единицы. Здесь проверен только источник вставки. "
        "Реконструкция воспроизводит длину и положение между равномерными окнами. Полный текст исходного эпизода не установлен.",
        "",
        "| Документ | Тип | Фразы | Позиция, слов | Общие слова подряд | Целая фраза | Подсвечено / слов участка | Найдено дословных / приблизительных |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        metric_lines(bundle["reconstruction"]),
        "",
        "## Условия воспроизведения",
        "",
        "Для каждой проверки подаются одни и те же 12 сохранённых страниц. Три публикации, откуда взяты сами "
        "входные документы, исключены из общей библиотеки. Для двух независимых рассказов об одном событии "
        "одно место из двенадцати отведено второму рассказу. При подсчёте покрытия проверяются позиции "
        "соответствующих слов и в проверяемом документе, и в нужной редакции источника. "
        "Время отдельного прогона зависит от нагрузки компьютера.",
        "",
        f"Контрольная сумма разметки DEV `{bundle['dev_key_sha256']}`. Версия сопоставления `{bundle['matcher']}`.",
        "",
        "Из корня проекта с подготовленным каталогом dev-30 можно повторить расчёт.",
        "",
        "```sh",
        "python3 scripts/compare_web_queries_offline.py --output /tmp/istok-dev-query-results.json --table /tmp/istok-dev-query-results.md",
        "```",
        ""])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, default=DEV)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--table", type=Path, help="Таблица для сравнения по каждому документу.")
    parser.add_argument("--probe-text", type=Path, help="Сохранить слепой текст реконструкции 40/710.")
    args = parser.parse_args()
    bundle, probe = measure(args.dev.resolve())
    args.output.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    if args.table:
        args.table.write_text(render_table(bundle))
    if args.probe_text:
        args.probe_text.write_text(probe)
    print(f"30 настроечных документов, отдельно реконструкция 40/710. Результат в {args.output}")


if __name__ == "__main__":
    main()
