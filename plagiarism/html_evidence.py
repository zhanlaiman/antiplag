"""Bounded independent HTML evidence; no PDF spacing relaxation or I/O."""
from collections import Counter
from hashlib import sha256
import json
import re

POLICY = "html-semantic-notes-independent-v1"
VIEW_ID = "html-notes-v1"
VIEW_EXTRACTION = "html-parser-v3-semantic-notes"
PRIMARY_EXTRACTION = "html-parser-v2-implied-ends"
MAX_COMBINED = 200_000
MAX_MARKERS = 512
MAX_PROOF_BYTES = 65_536
UNIT = re.compile(r"\[(?:[0-9]+|[A-Za-z])\]")
HEX = re.compile(r"[a-f0-9]{64}")


def digest(text): return sha256(text.encode()).hexdigest()
def canonical(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def declares_html(view):
    """Unknown/malformed HTML declarations must never fall back to PDF proof."""
    provenance = view.get('provenance', {})
    if not isinstance(provenance, dict): return True
    return ('html_derivation' in provenance or str(provenance.get('extraction', '')).startswith('html-')
        or str(provenance.get('evidence_policy', '')).startswith('html-')
        or str(view.get('id', '')).endswith('-' + VIEW_ID))


def normalize_space(text):
    # Exact legacy HTML whitespace normalizer, not arbitrary PDF spacing.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def compact(text):
    offsets = [i for i, c in enumerate(text) if not c.isspace()]
    return "".join(text[i] for i in offsets), offsets


def project(primary, spans):
    parts = []; end = 0
    for start, stop in spans:
        parts.extend((primary[end:start], " ")); end = stop
    parts.append(primary[end:])
    return normalize_space("".join(parts))


def build_derivation(primary, clean, ranges, raw_sha):
    """Linear, conservative locator plus exact boundary-preserving reconstruction."""
    if (not isinstance(primary, str) or not isinstance(clean, str) or "\0" in primary + clean
            or len(primary) + len(clean) > MAX_COMBINED or len(clean.strip()) < 35 or primary == clean
            or not isinstance(raw_sha, str) or not HEX.fullmatch(raw_sha)
            or not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_MARKERS):
        raise ValueError("HTML view bounds/identity")
    units = Counter()
    for row in ranges:
        marker = row.get("rendered_marker") if isinstance(row, dict) else None
        if not isinstance(marker, str) or not 1 <= len(marker) <= 64: raise ValueError("Raw marker bound")
        value = "".join(c for c in marker if not c.isspace())
        found = UNIT.findall(value)
        if not found or "".join(found) != value or len(value) > 64: raise ValueError("Not a bounded marker group")
        units.update(found)
    if sum(units.values()) > MAX_MARKERS: raise ValueError("Marker transcript bound")
    remaining = units.copy(); a, offsets = compact(primary); b, _ = compact(clean)
    i = j = 0; spans = []
    while i < len(a):
        if j < len(b) and a[i] == b[j]: i += 1; j += 1; continue
        marker = UNIT.match(a, i)
        if marker is None or remaining[marker.group()] <= 0 or len(marker.group()) > 64: raise ValueError("Ambiguous/non-marker edit; retain primary")
        remaining[marker.group()] -= 1
        spans.append([offsets[i], offsets[marker.end()-1] + 1]); i = marker.end()
        if len(spans) > MAX_MARKERS: raise ValueError("Marker removal bound")
    if j != len(b) or not spans or project(primary, spans) != clean:
        raise ValueError("Existing lexical whitespace/characters changed")
    result = {"policy": POLICY, "version": 1, "raw_html_sha256": raw_sha,
        "primary_text_sha256": digest(primary), "view_text_sha256": digest(clean),
        "primary_extraction": PRIMARY_EXTRACTION, "view_extraction": VIEW_EXTRACTION,
        "eligible_marker_units": dict(sorted(units.items())), "accepted_raw_ranges": len(ranges),
        "mask_ranges_sha256": digest(canonical(ranges)), "removed_primary_spans": spans, "removed_units": len(spans)}
    if len(canonical(result).encode()) > MAX_PROOF_BYTES: raise ValueError("Proof byte bound")
    return result


def valid_derivation(primary, clean, value):
    keys = {"policy", "version", "raw_html_sha256", "primary_text_sha256", "view_text_sha256", "primary_extraction", "view_extraction",
            "eligible_marker_units", "accepted_raw_ranges", "mask_ranges_sha256", "removed_primary_spans", "removed_units"}
    try:
        if (not isinstance(value, dict) or set(value) != keys or value["policy"] != POLICY or type(value["version"]) is not int or value["version"] != 1
                or value["primary_extraction"] != PRIMARY_EXTRACTION or value["view_extraction"] != VIEW_EXTRACTION
                or len(primary) + len(clean) > MAX_COMBINED or len(clean.strip()) < 35 or "\0" in primary + clean
                or value["primary_text_sha256"] != digest(primary) or value["view_text_sha256"] != digest(clean)
                or not all(isinstance(value[k], str) and HEX.fullmatch(value[k]) for k in ("raw_html_sha256", "mask_ranges_sha256"))
                or type(value["accepted_raw_ranges"]) is not int or not 1 <= value["accepted_raw_ranges"] <= MAX_MARKERS): return False
        units = value["eligible_marker_units"]; spans = value["removed_primary_spans"]
        if (not isinstance(units, dict) or not 1 <= len(units) <= MAX_MARKERS or any(not isinstance(k, str) or not UNIT.fullmatch(k) or len(k) > 64 or type(v) is not int or not 1 <= v <= MAX_MARKERS for k,v in units.items())
                or sum(units.values()) > MAX_MARKERS or not isinstance(spans, list) or not 1 <= len(spans) <= MAX_MARKERS
                or type(value["removed_units"]) is not int or value["removed_units"] != len(spans)): return False
        remaining = Counter(units); end = 0
        for span in spans:
            if not isinstance(span, list) or len(span) != 2 or any(type(x) is not int for x in span): return False
            start, stop = span
            if not end <= start < stop <= len(primary) or stop-start > 64: return False
            marker = "".join(c for c in primary[start:stop] if not c.isspace())
            if not UNIT.fullmatch(marker) or remaining[marker] <= 0: return False
            remaining[marker] -= 1; end = stop
        return project(primary, spans) == clean and len(canonical(value).encode()) <= MAX_PROOF_BYTES
    except (ValueError, TypeError, KeyError, UnicodeError): return False


def source_view(primary, fetched):
    """Validate one Fetcher-produced view; existing primary dict is not mutated."""
    from plagiarism.matching import MAX_TEXT_CHARS
    value = fetched.get("html_semantic_view")
    if fetched.get("content_type") != "text/html" or not isinstance(value, dict): return []
    text = value.get("text"); raw = fetched.get("raw_html_sha256"); proof = value.get("derivation")
    if (value.get("id") != VIEW_ID or value.get("policy") != POLICY or value.get("extraction") != VIEW_EXTRACTION
            or fetched.get("extraction") != PRIMARY_EXTRACTION or not isinstance(text, str)
            or len(primary["text"]) + len(text) > MAX_TEXT_CHARS or value.get("text_sha256") != digest(text)
            or not isinstance(raw, str) or not HEX.fullmatch(raw) or value.get("raw_html_sha256") != raw
            or not valid_derivation(primary["text"], text, proof) or proof["raw_html_sha256"] != raw): return []
    return [{"id": primary["id"] + "-" + VIEW_ID, "representation_of": primary["id"], "title": primary["title"], "url": primary["url"], "text": text,
        "provenance": {**primary.get("provenance", {}), "text_sha256": digest(text), "evidence_policy": POLICY,
            "extraction": VIEW_EXTRACTION, "raw_html_sha256": raw, "html_derivation": proof}}]


class HTMLSupport:
    def __init__(self, primary, view):
        self.primary, self.view = primary, view
        self.valid = False; self.derivation = None
        try:
            provenance = view.get("provenance", {}); self.derivation = provenance.get("html_derivation")
            self.valid = (view.get("representation_of") == primary.get("id") and view.get("url") == primary.get("url")
                and provenance.get("evidence_policy") == POLICY and provenance.get("extraction") == VIEW_EXTRACTION
                and provenance.get("text_sha256") == digest(view["text"])
                and valid_derivation(primary["text"], view["text"], self.derivation)
                and provenance.get("raw_html_sha256") == self.derivation["raw_html_sha256"])
        except (AttributeError, KeyError, TypeError, UnicodeError, ValueError): pass
    def evidence(self, match):
        if not self.valid or match.get("kind") != "exact" or match.get("source_id") != self.view["id"] or match.get("source_url") != self.view["url"]: return None
        from plagiarism.pdf_evidence import invalid_evidence_character
        if any(invalid_evidence_character(c) for field in ('excerpt', 'source_excerpt') for c in match.get(field, '')): return None
        start, end = match.get("source_start"), match.get("source_end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(self.view["text"]) or match.get("source_excerpt") != self.view["text"][start:end]: return None
        return {"policy": POLICY, "primary_source_id": self.primary["id"], "raw_html_sha256": self.derivation["raw_html_sha256"],
            "primary_text_sha256": digest(self.primary["text"]), "view_text_sha256": digest(self.view["text"]),
            "derivation_sha256": digest(canonical(self.derivation)), "view_span": [start, end], "view_excerpt_sha256": digest(match["source_excerpt"])}
