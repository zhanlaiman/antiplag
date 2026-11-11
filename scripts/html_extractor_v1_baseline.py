"""Frozen pre-fix HTML extractor for offline compatibility checks only."""
import re
from html.parser import HTMLParser
from plagiarism.web_fetch import FetchError


class LegacyTextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "form", "noscript", "template", "svg", "canvas", "iframe", "aside", "head", "title"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "section", "article", "li", "ul", "ol", "table", "tr", "br", "hr", "blockquote", "pre", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.pieces, self.title = [], [], []
        self.nodes = 0
        self.links, self.anchor = [], None

    def handle_starttag(self, tag, attributes):
        self.nodes += 1
        if self.nodes > 50_000 or len(self.stack) >= 128:
            raise FetchError("HTML страницы слишком сложный.", "html_too_complex")
        values = dict(attributes)
        if tag == "a":
            self.anchor = None
        style = values.get("style") or ""
        ignored = (tag in self.SKIP or "hidden" in values or str(values.get("aria-hidden", "")).lower() == "true"
                   or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I))
                   or values.get("role") in {"navigation", "banner", "contentinfo", "search"}
                   or bool(re.search(r"(?:^|\s)(?:cookie-banner|cookie-consent|site-header|site-footer)(?:\s|$)",
                                     (values.get("class") or "") + " " + (values.get("id") or ""), re.I)))
        if tag == "a" and not ignored and not any(frame[1] for frame in self.stack) and len(self.links) < 1024:
            href = values.get("href", "")
            if href and len(href) <= 4096:
                self.anchor = {"url": href, "text": ""}
        if tag in self.BLOCK and not ignored and not any(frame[1] for frame in self.stack):
            self.pieces.append("\n\n")
        if tag not in self.VOID:
            self.stack.append((tag, ignored))

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append(self.anchor)
            self.anchor = None
        if tag in self.BLOCK and not any(frame[1] for frame in self.stack):
            self.pieces.append("\n\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.anchor is not None and not any(frame[1] for frame in self.stack):
            self.anchor["text"] = (self.anchor["text"] + data)[:500]
        if any(tag == "title" for tag, _ in self.stack) and all(not ignored or tag in {"head", "title"} for tag, ignored in self.stack):
            if sum(map(len, self.title)) < 1000:
                self.title.append(data[:1000])
        elif not any(frame[1] for frame in self.stack):
            self.pieces.append(data)
