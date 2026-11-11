#!/usr/bin/env python3
"""One first-use synthetic matcher check on pre-reserved public page snapshots."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism import matching
from scripts.validate_matcher_variants import baseline


def main():
    folder = ROOT / "data/private/web-evaluation/matcher-unfamiliar-holdout"
    output = folder / "first-use.json"
    if output.exists():
        raise SystemExit("First-use result already exists; do not rerun or overwrite it.")
    reservation = json.loads((folder / "reservation.json").read_text())
    selected = {"name": matching.MATCHER_VERSION,
        "sha256": sha256((ROOT / "plagiarism/matching.py").read_bytes()).hexdigest(),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "reason": "DEV-30 and targeted live D10/D13 validation; holdout content not inspected for selection"}
    if reservation["selected_variant"] is None:
        reservation["selected_variant"] = selected
    else:
        assert reservation["selected_variant"]["sha256"] == selected["sha256"]
        reservation["preparation_note"] = "First sampling attempt found no 45-word single line and stopped before any matching. Same selected hash resumed with deterministic full-text fallback."
    (folder / "reservation.json").write_text(json.dumps(reservation, indent=2))
    old = baseline("a5a9aac")
    controls = [
        ["An embedded database can be useful when a small program needs to keep local records. Choosing the storage format depends on reliability requirements and the number of concurrent writers.",
         "Our application saves a list of books in one local file. A library volunteer can search titles without setting up a separate database server."],
        ["The water beside a pier can rise or fall during the day. Coastal measurements help a harbour operator plan when to move a boat through shallow channels.",
         "A tide gauge records how the sea surface moves relative to a fixed point. A long observation series can reveal local patterns that matter for navigation."],
        ["Watching an eclipse requires appropriate eye protection. Ordinary sunglasses are a poor choice for direct observation, so a visitor should follow instructions provided by the event organiser.",
         "The astronomy club prepared a shaded projection screen for the solar event. Volunteers explained the viewing method before letting school groups approach the equipment."],
    ]
    filler_a = "Our editorial team wrote this independent introduction to describe a local planning exercise. The participants compared their notes, discussed the schedule, and agreed to preserve the final record."
    filler_b = "After the discussion the secretary prepared a clean copy for the archive. The meeting ended before noon and everyone checked the room before leaving."
    rows = []
    for index, entry in enumerate(reservation["fetches"]):
        assert "saved_path" in entry
        page = json.loads((folder / entry["saved_path"]).read_text())
        assert page["text_sha256"] == entry["sha256"] == sha256(page["text"].encode()).hexdigest()
        paragraphs = list(re.finditer(r"[^\n]+", page["text"]))
        paragraph = next((p for p in paragraphs if len(matching._tokens(p.group())) >= 45), None)
        passage_start = paragraph.start() if paragraph else 0
        tokens = matching._tokens(paragraph.group() if paragraph else page["text"])
        source = {"id": "heldout-" + str(index), "url": page["url"], "title": page["title"], "text": page["text"]}
        for length in (10, 20, 40):
            start = passage_start + tokens[0].start
            end = passage_start + tokens[length - 1].end
            quote = page["text"][start:end]
            for kind in ("literal_copy", "one_added_word"):
                insertion = quote
                if kind == "one_added_word":
                    cut = matching._tokens(quote)[length - 4].start
                    insertion = quote[:cut] + "additionally " + quote[cut:]
                target = filler_a + "\n\n" + insertion + "\n\n" + filler_b
                rows.append(score(old, source, target, {"id": str(index) + "-" + kind + "-" + str(length),
                    "kind": kind, "synthetic": True, "positive": True, "source_span": [start, end],
                    "expected_copied_words": length, "source_quote": quote}))
        for number, target in enumerate(controls[index]):
            rows.append(score(old, source, target, {"id": str(index) + "-control-" + str(number),
                "kind": "independent_same_topic", "synthetic": True, "positive": False}))
    result = {"reserved_at": reservation["reserved_at"], "selected_variant": reservation["selected_variant"],
              "evaluated_at": datetime.now(timezone.utc).isoformat(), "search_queries": 0, "paid_api_calls": 0,
              "used_for_tuning": False, "baseline": old.MATCHER_VERSION, "cases": rows}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)); output.chmod(0o600)
    for kind in ("literal_copy", "one_added_word", "independent_same_topic"):
        cases = [r for r in rows if r["kind"] == kind]
        print(kind, len(cases), {version: sum(r[version].get("source_words_covered", 0) for r in cases)
              for version in ("before", "after")}, "highlighted_controls", sum(bool(r["after"]["matches"]) for r in cases if not r["positive"]))


def score(old, source, target, row):
    row["target"] = target
    row["source_url"] = source["url"]
    for name, module in (("before", old), ("after", matching)):
        matches = module.analyze(target, [source])["matches"]
        result = {"matches": matches}
        if row["positive"]:
            a, b = row["source_span"]
            wanted = [t for t in module._tokens(source["text"]) if a <= t.start and t.end <= b]
            result["source_words_covered"] = sum(any(m["kind"] == "exact" and m["source_start"] <= t.start
                and t.end <= m["source_end"] for m in matches) for t in wanted)
        row[name] = result
    return row


if __name__ == "__main__":
    main()
