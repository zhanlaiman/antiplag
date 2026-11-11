#!/usr/bin/env python3
"""Local CSS HTML structure probe; no publication prose is emitted or retained.

CSS is explicitly used for HTML tuning. RFC sources remain unopened. This
diagnostic subclass is not the production parser and performs no network.
"""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plagiarism.web_fetch import _TextExtractor, Fetcher, FetchError

BASE = ROOT / "data/private/web-evaluation/query-unfamiliar-reserved-v2"


# The implementation is now in the production extractor; preserve the old
# callback stack separately so the saved-byte comparison remains reproducible.
from scripts.html_extractor_v1_baseline import LegacyTextExtractor
OptionalClosingExtractor = _TextExtractor


def css_structure_probe():
    reservation = json.loads((BASE / "reservation.json").read_text())
    if not reservation.get("source_tuning_status", {}).get("CSSOverview", {}).get("used_for_tuning"):
        raise ValueError("CSS must be marked used for HTML tuning before structural analysis")
    source = json.loads((BASE / "CSSOverview-attempt.json").read_text())
    response = json.loads((BASE / source["last_http_response_artifact"]).read_text())
    if response["url"] != "https://www.w3.org/Style/CSS/Overview.en.html":
        raise ValueError("This probe may open only the captured CSS HTML")
    path = (BASE / source["raw_body_path"]).resolve()
    if path.parent != BASE.resolve() or not path.name.startswith("CSSOverview-http-"):
        raise ValueError("CSS raw response identity changed")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != source["raw_body_sha256"]:
        raise ValueError("Captured CSS bytes changed")
    decoded, _ = Fetcher._decode(raw, response["headers"]["Content-Type"])
    results = {}
    for name, parser_class in (("previous_v1", LegacyTextExtractor), ("implied_ends_candidate", OptionalClosingExtractor)):
        class NoProse(parser_class):
            def __init__(self):
                super().__init__()
                self.maximum_depth = 0
                self.visible_characters = 0
                self.implicit_closures = Counter()
            def _close_scoped(self, closing, boundaries):
                before = list(self.stack)
                super()._close_scoped(closing, boundaries)
                if len(self.stack) < len(before):
                    self.implicit_closures[before[len(self.stack)][0]] += 1
            def handle_starttag(self, tag, attributes):
                super().handle_starttag(tag, attributes)
                self.maximum_depth = max(self.maximum_depth, len(self.stack))
            def handle_data(self, data):
                # Count characters only. Do not keep text/title/anchor prose.
                if not any(ignored for _, ignored in self.stack):
                    self.visible_characters += len(data)
        parser = NoProse()
        state, code = "complete", None
        try:
            parser.feed(decoded)
            parser.close()
        except FetchError as error:
            state, code = "failed", error.code
        results[name] = {"state": state, "code": code, "nodes_seen": parser.nodes,
            "maximum_stack_depth": parser.maximum_depth, "stack_depth_at_stop": len(parser.stack),
            "open_tag_counts_at_stop": dict(Counter(tag for tag, _ in parser.stack)),
            "visible_characters_count_only": parser.visible_characters,
            "implicit_closures": dict(getattr(parser, "implicit_closures", {}))}
    result = {"schema_version": 1, "source_id": "CSSOverview", "used_for_tuning": True,
        "prose_inspected": False, "network_calls": 0, "raw_body_sha256": source["raw_body_sha256"], "results": results}
    path = BASE / "optional-closing-structure-probe.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)
    return result


if __name__ == "__main__":
    print(json.dumps(css_structure_probe(), ensure_ascii=False, indent=2))
