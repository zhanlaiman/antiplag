"""Restricted supplementary PDF evidence without replacing the primary text."""
from hashlib import sha256

from plagiarism.matching import _coverage, _deduplicate, analyze

POLICY = "pdfium-spacing-primary-order-v1"


def invalid_evidence_character(character):
    value = ord(character)
    return (value == 0 or 0xD800 <= value <= 0xDFFF or 0xFDD0 <= value <= 0xFDEF
            or value & 0xFFFF in (0xFFFE, 0xFFFF))


class PrimarySupport:
    """Index once; permit whitespace changes, never changed/reordered letters."""
    def __init__(self, text, identifier):
        self.text, self.identifier = text, identifier
        self.compact = "".join(c for c in text if not c.isspace())
        self.offsets = [i for i, c in enumerate(text) if not c.isspace()]
        self.digest = sha256(text.encode()).hexdigest()

    def evidence(self, match):
        if match.get("kind") != "exact":
            return None
        if any(invalid_evidence_character(c) for field in ("excerpt", "source_excerpt")
               for c in match[field]):
            return None
        wanted = "".join(c for c in match["source_excerpt"] if not c.isspace())
        position = self.compact.find(wanted)
        if not wanted or position < 0:
            return None
        start, end = self.offsets[position], self.offsets[position + len(wanted) - 1] + 1
        return {"policy": POLICY, "primary_source_id": self.identifier,
                "primary_span": [start, end], "primary_text_sha256": self.digest,
                "primary_excerpt_sha256": sha256(self.text[start:end].encode()).hexdigest(),
                "ordered_characters_sha256": sha256(wanted.encode()).hexdigest()}


def analyze_with_supplements(text, sources, supplements=()):
    """Keep primary results and admit only corroborated supplementary exacts.

    Every representation is stored as a complete independent snapshot; matching
    never crosses from one extracted view into another. Physical page counters
    count the original sources, and overlapping words are still counted once.
    """
    sources, supplements = list(sources), list(supplements)
    result = analyze(text, sources + supplements)
    if not supplements:
        return result
    primary = {s["id"]: s for s in sources}
    secondary = {s["id"]: s for s in supplements}
    support = {}
    kept = []
    for match in result["matches"]:
        view = secondary.get(match["source_id"])
        if view is None:
            kept.append(match)
            continue
        identifier = view.get("representation_of")
        original = primary.get(identifier)
        if original is None or view.get("url") != original.get("url"):
            continue
        if identifier not in support:
            support[identifier] = PrimarySupport(original["text"], identifier)
        proof = support[identifier].evidence(match)
        if proof is not None:
            kept.append({**match, "pdf_spacing_evidence": proof})
    # A precise corroborated view can also remove a broad primary fuzzy span
    # whose entire source is already accounted for by the exact quotation.
    anchors = [(m, m["pdf_spacing_evidence"]) for m in kept if m.get("pdf_spacing_evidence")]
    kept = [candidate for candidate in kept if not (
        candidate["kind"] == "similar" and any(
            proof["primary_source_id"] == candidate["source_id"]
            and candidate["start"] <= anchor["start"] and anchor["end"] <= candidate["end"]
            and proof["primary_span"][0] <= candidate["source_start"]
            and proof["primary_span"][1] >= candidate["source_end"]
            for anchor, proof in anchors))]
    grouped = {}
    for match in kept:
        grouped.setdefault(match["source_url"], []).append(match)
    matches = [m for group in grouped.values() for m in _deduplicate(group)]
    matches.sort(key=lambda m: (m["start"], -m["score"], str(m["source_id"])))
    for number, match in enumerate(matches, 1):
        match["id"] = f"match-{number}"
    result["matches"] = matches
    covered = _coverage(matches)
    result["stats"].update(matched_characters=covered,
        similarity_percent=round(100 * covered / len(text), 1),
        source_count=len({m["source_url"] for m in matches}), checked_source_count=len(sources))
    return result
