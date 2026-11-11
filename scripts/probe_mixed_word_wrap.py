#!/usr/bin/env python3
"""Local-only seeded token-edge prototype; production matcher is never edited.

Each token boundary offers its original word and at most three joined parts
through literal hyphen + one line ending. Graph states have explicit bounds;
there is no enumeration of whole-document keep/join combinations.
"""
import argparse
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
from pathlib import Path
import re
import resource
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.validate_matcher_variants import artificial_pairs, baseline, coverage, dev_audit, source

BASELINE_REF = "43b8039"
MAX_JOIN_PARTS = 4
MAX_JOIN_CHARACTERS = 128
MAX_STATES = 60_000
BENCHMARK_TIMEOUT_SECONDS = 20
LINE_WRAP = re.compile(r"-[ \t]*(?:\r\n|\n|\r)[ \t]*")


class TokenEdges:
    def __init__(self, module, text):
        self.text, self.tokens = text, module._tokens(text)
        self.cached = {}

    def can_join(self, left, right):
        a, b = self.tokens[left], self.tokens[right]
        return a.value[-1].isalpha() and b.value[0].isalpha() and LINE_WRAP.fullmatch(self.text[a.end:b.start])

    def edges(self, index, direction):
        key = index, direction
        if key in self.cached:
            return self.cached[key]
        tokens, result = self.tokens, []
        if direction == 1 and index < len(tokens):
            value = tokens[index].value
            result.append((value, index + 1))
            for stop in range(index + 2, min(len(tokens), index + MAX_JOIN_PARTS) + 1):
                if not self.can_join(stop - 2, stop - 1):
                    break
                value += tokens[stop - 1].value
                if len(value) > MAX_JOIN_CHARACTERS:
                    break
                result.append((value, stop))
        elif direction == -1 and index > 0:
            value = tokens[index - 1].value
            result.append((value, index - 1))
            for start in range(index - 2, max(-1, index - MAX_JOIN_PARTS - 1), -1):
                if not self.can_join(start, start + 1):
                    break
                value = tokens[start].value + value
                if len(value) > MAX_JOIN_CHARACTERS:
                    break
                result.append((value, start))
        self.cached[key] = result
        return result


def walk(target, other, initial, direction, *, end=None, diagnostics=None):
    """Visit token-index pairs once; equal edge values cannot create cycles."""
    pending, seen, best = deque([initial]), {initial}, initial
    hit_limit = False
    while pending:
        i, j = pending.popleft()
        if end is not None and (i, j) == end:
            if diagnostics is not None:
                diagnostics["edge_states"] += len(seen)
            return (i, j), True
        source_edges = {value: stop for value, stop in other.edges(j, direction)}
        for value, stop in target.edges(i, direction):
            other_stop = source_edges.get(value)
            if other_stop is None:
                continue
            if end is not None and (stop > end[0] or other_stop > end[1]):
                continue
            state = stop, other_stop
            if state in seen:
                continue
            if len(seen) >= MAX_STATES:
                hit_limit = True
                pending.clear()
                break
            seen.add(state)
            pending.append(state)
            if (direction == 1 and state > best) or (direction == -1 and state < best):
                best = state
    if diagnostics is not None:
        diagnostics["edge_states"] += len(seen)
        diagnostics["state_limit_hits"] += hit_limit
    return best, end is None and not hit_limit


def edge_equivalent(module, first, second):
    if module.literal_equivalent(first, second):
        return True
    target, other = TokenEdges(module, first), TokenEdges(module, second)
    if not target.tokens or not other.tokens:
        return False
    end = len(target.tokens), len(other.tokens)
    _, found = walk(target, other, (0, 0), 1, end=end)
    return found


def seed_is_long(module, match):
    def forms(text):
        tokens = module._tokens(text)
        return {tuple(t.value for t in tokens), tuple(t.value for t in module._joined_line_tokens(text, tokens))}
    common = forms(match["excerpt"]) & forms(match["source_excerpt"])
    return (match["end"] - match["start"] >= module.MIN_CHARACTERS
            and any(len(words) >= module.MIN_WORDS for words in common))


def make_prototype():
    module = baseline(BASELINE_REF)
    original_analyze = module.analyze
    module.MATCHER_VERSION += "+seeded-local-wrap-edges-prototype"
    module.probe_diagnostics = []

    def analyze(text, sources):
        result = original_analyze(text, sources)
        diagnostic = {"anchors_considered": 0, "anchors_extended": 0,
                      "edge_states": 0, "state_limit_hits": 0}
        target, combined = None, []
        for item in sources:
            old = [m for m in result["matches"] if m["source_id"] == item["id"]]
            exact = [m for m in old if m["kind"] == "exact"]
            if not exact or not (LINE_WRAP.search(text) or LINE_WRAP.search(item["text"])):
                combined.extend(old)
                continue
            # A full existing span cannot gain target coverage for this source.
            if any(not re.search(r"[^\W_]", text[:m["start"]] + text[m["end"]:]) for m in exact):
                combined.extend(old)
                continue
            if target is None:
                target = TokenEdges(module, text)
            other = TokenEdges(module, item["text"])
            target_starts = {t.start: i for i, t in enumerate(target.tokens)}
            target_ends = {t.end: i + 1 for i, t in enumerate(target.tokens)}
            source_starts = {t.start: i for i, t in enumerate(other.tokens)}
            source_ends = {t.end: i + 1 for i, t in enumerate(other.tokens)}
            extended = []
            for anchor in sorted(exact, key=lambda m: (m["start"], -m["end"])):
                diagnostic["anchors_considered"] += 1
                if any(m["start"] <= anchor["start"] and anchor["end"] <= m["end"]
                       and m["source_start"] <= anchor["source_start"] and anchor["source_end"] <= m["source_end"]
                       for m in extended) or not seed_is_long(module, anchor):
                    continue
                begin = target_starts[anchor["start"]], source_starts[anchor["source_start"]]
                finish = target_ends[anchor["end"]], source_ends[anchor["source_end"]]
                before, _ = walk(target, other, begin, -1, diagnostics=diagnostic)
                after, _ = walk(target, other, finish, 1, diagnostics=diagnostic)
                if before == begin and after == finish:
                    continue
                # Validate the combined original span through the same bounded
                # graph, including the seed, before accepting any new evidence.
                _, proven = walk(target, other, before, 1, end=after, diagnostics=diagnostic)
                if not proven:
                    continue
                match = module._make_match(text, item, target.tokens, other.tokens,
                    before[0], after[0], before[1], after[1], "exact", 1.0)
                match["normalization"] = "single-line-wrap-local-edges-prototype"
                extended.append(match)
                diagnostic["anchors_extended"] += 1
            combined.extend(module._deduplicate(old + extended))
        combined.sort(key=lambda m: (m["start"], -m["score"], str(m["source_id"])))
        for index, match in enumerate(combined, 1):
            match["id"] = "match-" + str(index)
        result["matches"] = combined
        result["stats"]["matched_characters"] = module._coverage(combined)
        result["stats"]["similarity_percent"] = round(100 * result["stats"]["matched_characters"] / len(text), 1)
        result["stats"]["source_count"] = len({m["source_id"] for m in combined})
        module.probe_diagnostics.append(diagnostic)
        return result

    module.analyze = analyze
    # Keep production's existing original/global interpretations as valid and
    # add a strictly bounded local-edge equality test for prototype spans.
    base_for_equality = baseline(BASELINE_REF)
    module.literal_equivalent = lambda a, b: edge_equivalent(base_for_equality, a, b)
    return module


def extra_pairs():
    mixed = "Scientists measured long-term changes in extremely distant galaxies."
    no_seed = "Our long-term plan considers extremely distant galaxies."
    rows = []
    for name, text in (("mixed-with-seed", mixed), ("mixed-without-surviving-global-seed", no_seed)):
        rows.append({"id": name, "kind": "mixed_line_wrap", "synthetic": True, "positive": True,
            "target": text, "source": text.replace("long-term", "long-\nterm").replace("extremely", "ex-\ntremely")})
    rows.append({"id": "genuine-compound-preserved", "kind": "genuine_compound", "synthetic": True,
        "positive": True, "target": mixed, "source": mixed.replace("long-term", "long-\nterm")})
    negatives = [
        ("The tele-\nscope collected detailed images of a distant star before sunrise.",
         "An observatory can measure the brightness of astronomical objects using calibrated sensors."),
        ("Coastal obser-\nvations help a harbour pilot estimate the depth near a shallow channel.",
         "A tide gauge records changes in sea level at a fixed place beside the shore."),
        ("The photo-\ngrapher adjusted the lens before taking a portrait beside the window.",
         "Exposure time and aperture influence how a camera records available light."),
        ("The pro-\ngram keeps a small list of borrowed books in a local file.",
         "A database transaction groups related updates so that they succeed or fail together."),
        ("The ther-\nmometer was moved into the shade after the morning inspection.",
         "Measurement uncertainty expresses a range associated with an observed quantity."),
        ("The astro-\nnomy club prepared a projection screen for the solar event.",
         "Direct viewing of the Sun requires suitable protection for the observer's eyes."),
    ]
    rows.extend({"id": "wrapped-independent-" + str(i), "kind": "independent_wrapped_same_topic", "synthetic": True,
                 "positive": False, "target": a, "source": b} for i, (a, b) in enumerate(negatives, 1))
    return rows


def stress_texts(kind):
    if kind == "mixed":
        first = "Scientists measured long-term changes in extremely distant galaxies.\n"
        second = first.replace("long-term", "long-\nterm").replace("extremely", "ex-\ntremely")
    elif kind == "plain":
        first = second = "Researchers carefully inspected the detailed measurements before publishing their final observations.\n"
    else:
        first = "Scientists carefully observed the distant galaxies and recorded their positions after midnight.\n"
        second = "Our coastal laboratory monitors local water conditions using independent temperature sensors near the harbour.\n"
    count = 199_900 // max(len(first), len(second))
    return first * count, second * count


def benchmark(variant, kind):
    module = baseline(BASELINE_REF) if variant == "baseline" else make_prototype()
    # Correctness above stays pinned to v4. Performance comparisons apply the
    # separately verified v4.1 cache to both arms, avoiding the known unrelated
    # whole-excerpt retokenization pathology in the old baseline.
    from plagiarism import matching as current
    if current.MATCHER_VERSION != "lexical-v4.1-cached-anchors":
        raise RuntimeError("Benchmark requires the verified v4.1 anchor-cache reference")
    anchor_function = inspect.getsource(current._similar_matches)
    exec(compile(anchor_function, "benchmark-only:v4.1-cached-anchors", "exec"), module.__dict__)
    text, other = stress_texts(kind)
    started = time.perf_counter()
    result = module.analyze(text, [source(other, "stress")])
    elapsed = time.perf_counter() - started
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    tokens = module._tokens(text)
    exact = [m for m in result["matches"] if m["kind"] == "exact"]
    print(json.dumps({"variant": variant, "case": kind, "target_characters": len(text), "source_characters": len(other),
        "anchor_cache_reference": current.MATCHER_VERSION, "anchor_function_sha256": sha256(anchor_function.encode()).hexdigest(),
        "elapsed_seconds": round(elapsed, 4), "peak_process_rss_mib": round(rss / 1024 / 1024, 2),
        "expected_words": len(tokens), "exact_words": sum(any(m["start"] <= t.start and t.end <= m["end"] for m in exact) for t in tokens),
        "exact_spans": len(exact), "diagnostics": getattr(module, "probe_diagnostics", [])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-benchmarks", action="store_true")
    parser.add_argument("--benchmark-variant", choices=("baseline", "prototype"))
    parser.add_argument("--benchmark-case", choices=("mixed", "plain", "independent"))
    args = parser.parse_args()
    if args.benchmark_variant:
        benchmark(args.benchmark_variant, args.benchmark_case)
        return
    if not args.output:
        parser.error("Private output is required")
    expected_parent = (ROOT / "data/private/web-evaluation").resolve()
    if args.output.resolve().parent != expected_parent:
        parser.error("Prototype output must stay in private web-evaluation")
    dev, reports = expected_parent / "materials/dev-30", expected_parent / "dev-live-30-v1"
    old, new = baseline(BASELINE_REF), make_prototype()
    rows, cases = dev_audit(old, new, dev, reports)
    pairs = artificial_pairs(cases, dev) + extra_pairs()
    for pair in pairs:
        for name, module in (("before", old), ("after", new)):
            pair[name] = coverage(module, pair["target"], [source(pair["source"])])
            pair[name]["exact_words"] = len(pair[name].pop("exact_token_indices"))
            pair[name]["target_words"] = len(module._tokens(pair["target"]))
    benches = []
    # Save correctness evidence before a deliberately adversarial stress run.
    # A baseline timeout must never discard the completed DEV comparison.
    initial = {"dev_cases": rows, "artificial_pairs": pairs, "benchmarks": benches,
               "phase": "correctness_complete_benchmarks_pending"}
    args.output.write_text(json.dumps(initial, ensure_ascii=False, indent=2) + "\n"); args.output.chmod(0o600)
    if not args.skip_benchmarks:
        for kind in ("mixed", "plain", "independent"):
            for variant in ("baseline", "prototype"):
                started = time.perf_counter()
                try:
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                        "--benchmark-variant", variant, "--benchmark-case", kind], cwd=ROOT,
                        capture_output=True, text=True, timeout=BENCHMARK_TIMEOUT_SECONDS, check=True)
                    benches.append(json.loads(completed.stdout))
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
                    benches.append({"variant": variant, "case": kind,
                        "outcome": "timeout" if isinstance(error, subprocess.TimeoutExpired) else "process_failed",
                        "elapsed_seconds_at_least": round(time.perf_counter() - started, 3),
                        "timeout_seconds": BENCHMARK_TIMEOUT_SECONDS})
    result = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_ref": BASELINE_REF, "baseline_version": old.MATCHER_VERSION, "prototype_version": new.MATCHER_VERSION,
        "production_edited": False, "network_calls": 0, "models_called": 0, "closed_final_materials_read": False,
        "bounds": {"join_parts": MAX_JOIN_PARTS, "joined_word_characters": MAX_JOIN_CHARACTERS,
                   "states_per_walk": MAX_STATES, "minimum_seed_words": old.MIN_WORDS, "minimum_seed_characters": old.MIN_CHARACTERS,
                   "benchmark_timeout_seconds": BENCHMARK_TIMEOUT_SECONDS},
        "dev_cases": rows, "artificial_pairs": pairs, "benchmarks": benches, "diagnostics": new.probe_diagnostics,
        "limitations": ["Existing six-word/35-character v4 seed required; a mixed-wrap copy without any surviving global seed remains missed.",
            "Four parts/128 characters per joined token and 60000 states per walk are explicit conservative bounds.",
            "Prototype equality is local to this experiment; production scoring/rendering is unchanged."]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n"); args.output.chmod(0o600)
    print(json.dumps({"dev_changed": [r["id"] for r in rows if r["added"] or r["removed"]],
        "dev_word_losses": [r["id"] for r in rows if r["after_exact_words"] < r["before_exact_words"]],
        "pairs": len(pairs), "pair_losses": [p["id"] for p in pairs if p["after"]["exact_words"] < p["before"]["exact_words"]],
        "independent_new_highlights": [p["id"] for p in pairs if not p["positive"] and p["after"]["matches"]],
        "extra_cases": [{"id": p["id"], "before": p["before"]["exact_words"], "after": p["after"]["exact_words"],
                         "target_words": p["after"]["target_words"]} for p in pairs if p["id"].startswith(("mixed-", "genuine-"))],
        "benchmarks": benches}, ensure_ascii=False))


if __name__ == "__main__":
    main()
