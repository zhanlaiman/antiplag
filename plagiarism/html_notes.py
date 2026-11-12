"""Conservatively mask bounded, explicitly marked HTML citation markers.

The caller must validate the original HTML with its existing text extractor
first. This module does not extract evidence, resolve URLs, or access I/O.
Returned offsets refer to decoded HTML character positions, not text or bytes.
"""
from html import unescape
from html.parser import HTMLParser
import re


NOTE_LINK = re.compile(r"^#cite_note(?:-[-\w:.]+)?$")
MARKER = re.compile(r"(?:\[\s*(?:[0-9]+|[A-Za-z])\s*\]\s*)+")
PROTECTED_ANCESTORS = frozenset({"code", "pre", "math", "kbd", "samp", "var", "textarea", "q", "blockquote"})


class HTMLNotesError(ValueError):
    """Bounded note inspection failed; the original extraction remains valid."""


class _Notes(HTMLParser):
    def __init__(self, source, void_tags):
        super().__init__(convert_charrefs=False)
        self.source, self.void = source, frozenset(void_tags)
        self.lines = [0] + [m.end() for m in re.finditer("\n", source)]
        self.active, self.ranges, self.ancestors = None, [], []
        self.nodes, self.protected_starts = 0, 0
        self.reasons = set()

    def source_offset(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def uncertain(self, reason):
        self.reasons.add(reason)
        if self.active is not None:
            self.active["valid"] = False

    def add(self, text):
        if self.active is not None:
            combined = self.active["visible"] + text
            if len(combined) > 64:
                self.active["overflow"] = True
            self.active["visible"] = combined[:65]

    def handle_starttag(self, tag, attrs):
        protected = any(t in PROTECTED_ANCESTORS for t in self.ancestors)
        was_active = self.active is not None
        self.nodes += 1
        if self.nodes > 50_000:
            raise HTMLNotesError("HTML note node inspection bound exceeded")
        values = dict(attrs)
        local = bool(NOTE_LINK.fullmatch(values.get("href") or "")) if tag == "a" else False
        if self.active is None:
            if local or tag == "sup" and "reference" in (values.get("class") or "").split():
                self.active = {"tag": tag, "start": self.source_offset(), "stack": [tag],
                    "visible": "", "has_local_note": local, "valid": True, "overflow": False}
        else:
            if tag not in {"a", "span", "sup"}:
                self.active["valid"] = False
            if local:
                self.active["has_local_note"] = True
            if tag not in self.void:
                self.active["stack"].append(tag)
            if len(self.active["stack"]) >= 128:
                raise HTMLNotesError("HTML note candidate depth bound exceeded")
        if self.active is not None and (protected or self.reasons):
            self.active["valid"] = False
            if protected and not was_active:
                self.protected_starts += 1
        if tag not in self.void and not self.reasons:
            if len(self.ancestors) >= 128:
                self.uncertain("ancestry_tracking_bound")
            else:
                self.ancestors.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in self.void:
            self.uncertain("self_closing_non_void")
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.reasons:
            if not self.ancestors or self.ancestors[-1] != tag:
                self.uncertain("mismatched_or_unopened_end")
            else:
                self.ancestors.pop()
        frame = self.active
        if frame is None:
            return
        if not frame["stack"] or frame["stack"][-1] != tag:
            frame["valid"] = False
            if tag == frame["tag"]:
                self.active = None
            return
        frame["stack"].pop()
        if frame["stack"]:
            return
        end = self.source.find(">", self.source_offset()) + 1
        visible = frame["visible"].strip()
        if (frame["valid"] and not frame["overflow"] and frame["has_local_note"] and end > frame["start"]
                and 0 < len(visible) <= 64 and MARKER.fullmatch(visible)):
            self.ranges.append({"start": frame["start"], "end": end, "tag": frame["tag"],
                "rendered_marker": visible, "reason": "semantic_local_citation_marker"})
        self.active = None

    def handle_data(self, data):
        self.add(data)

    def handle_entityref(self, name):
        self.add(unescape("&" + name + ";"))

    def handle_charref(self, name):
        self.add(unescape("&#" + name + ";"))

    def close(self):
        super().close()
        if self.ancestors or self.active is not None:
            self.uncertain("unclosed_ancestry_or_candidate")
        if self.reasons:
            self.ranges = []


def mask_semantic_notes(source, *, void_tags):
    """Return masked HTML, accepted ranges, and conservative ancestry audit."""
    if not isinstance(source, str):
        raise ValueError("Decoded HTML string required")
    detector = _Notes(source, void_tags)
    detector.feed(source)
    detector.close()
    output = list(source)
    for row in detector.ranges:
        for index in range(row["start"], row["end"]):
            if output[index] not in "\r\n":
                output[index] = " "
    masked = "".join(output)
    assert len(masked) == len(source)
    return masked, detector.ranges, {"ancestry_uncertain": bool(detector.reasons),
        "uncertainty_reasons": sorted(detector.reasons), "protected_candidate_starts": detector.protected_starts}
