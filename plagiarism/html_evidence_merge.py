"""Add exact HTML evidence after the unchanged primary/PDF analysis."""
from collections import Counter
from plagiarism.matching import MAX_SOURCES, _coverage, _tokens, analyze
from plagiarism.pdf_evidence import analyze_with_supplements as legacy_analyze
from plagiarism.html_evidence import HTMLSupport, POLICY, declares_html


def analyze_with_supplements(text, sources, supplements=()):
    sources, supplements = list(sources), list(supplements)
    if len(sources) + len(supplements) > MAX_SOURCES: raise ValueError('Independent source view cap')
    html = [s for s in supplements if declares_html(s)]
    pdf = [s for s in supplements if not declares_html(s)]
    result = legacy_analyze(text, sources, pdf)
    primary = {s['id']: s for s in sources}
    matches = list(result['matches'])  # Keep every legacy object, including similar/PDF/IDs.
    tokens = _tokens(text)
    coverage = {}
    for match in matches:
        if match['kind'] == 'exact':
            coverage.setdefault(match['source_url'], set()).update(t.start for t in tokens if match['start'] <= t.start and t.end <= match['end'])
    seen_parents = set(); identifiers = {m['id'] for m in matches}; next_id = 1
    # Ambiguous duplicate view declarations are not admitted.
    parent_counts = Counter(s.get('representation_of') for s in html)
    physical_urls = Counter(s.get('url') for s in sources)
    allowed = max(0, MAX_SOURCES - len(sources) - len(pdf))
    for view in html[:allowed]:
        parent = view.get('representation_of'); original = primary.get(parent)
        if original is None or parent in seen_parents or parent_counts[parent] != 1 or physical_urls[original.get('url')] != 1: continue
        seen_parents.add(parent); support = HTMLSupport(original, view)
        if not support.valid: continue
        for match in analyze(text, [view])['matches']:
            proof = support.evidence(match)
            if proof is None: continue
            covered = {t.start for t in tokens if match['start'] <= t.start and t.end <= match['end']}
            old = coverage.setdefault(match['source_url'], set())
            if not covered - old: continue
            while f'html-match-{next_id}' in identifiers: next_id += 1
            identifier = f'html-match-{next_id}'; next_id += 1; identifiers.add(identifier)
            matches.append({**match, 'id': identifier, 'html_semantic_notes_evidence': proof})
            old.update(covered)
    result['matches'] = matches
    covered = _coverage(matches)
    result['stats'].update(matched_characters=covered,
        similarity_percent=round(100 * covered / len(text), 1) if text else 0,
        source_count=len({m['source_url'] for m in matches}), checked_source_count=len(sources))
    return result
