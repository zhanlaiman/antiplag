"""Authored counterexamples for supplemental source proof and source counters."""
import hashlib
import unittest

from plagiarism.matching import analyze, _tokens
from plagiarism.pdf_evidence import PrimarySupport, analyze_with_supplements
from plagiarism.web_evaluation import _report


QUOTE = "Редактор проверил каждую строку перед публикацией подробного описания лабораторного измерения"


def sources(primary, other):
    return ([{"id": "primary", "url": "https://source.test/paper.pdf", "text": primary}],
            [{"id": "secondary", "representation_of": "primary", "url": "https://source.test/paper.pdf", "text": other}])


class SupplementalEvidenceTests(unittest.TestCase):
    def test_spacing_recovery_has_original_offsets_support_and_one_physical_source(self):
        primary = QUOTE.replace("лабораторного", "лабо раторного")
        base, extra = sources(primary, QUOTE)
        result = analyze_with_supplements(QUOTE, base, extra)
        exact = [m for m in result["matches"] if m["kind"] == "exact"]
        self.assertEqual(len(exact), 1)
        match = exact[0]
        self.assertEqual(match["excerpt"], QUOTE)
        self.assertEqual(match["source_excerpt"], extra[0]["text"][match["source_start"]:match["source_end"]])
        proof = match["pdf_spacing_evidence"]
        self.assertEqual(primary[slice(*proof["primary_span"])], primary)
        self.assertEqual(proof["primary_text_sha256"], hashlib.sha256(primary.encode()).hexdigest())
        self.assertEqual(result["stats"]["source_count"], 1)
        self.assertEqual(result["stats"]["checked_source_count"], 1)

    def test_changed_order_extra_letters_and_noncharacters_cannot_add_proof(self):
        base, extra = sources("Перед публикацией редактор проверил каждую строку подробного описания лабораторного измерения", QUOTE)
        baseline = analyze(QUOTE, base)
        result = analyze_with_supplements(QUOTE, base, extra)
        self.assertEqual(result["matches"], baseline["matches"])
        index = PrimarySupport(QUOTE, "primary")
        for bad in (QUOTE + " лишнее", QUOTE + "\ufffe", QUOTE + "\U0010ffff", QUOTE + "\ud800"):
            self.assertIsNone(index.evidence({"kind": "exact", "excerpt": QUOTE, "source_excerpt": bad}))
        self.assertIsNone(index.evidence({"kind": "similar", "excerpt": QUOTE, "source_excerpt": QUOTE}))

    def test_two_views_cannot_create_a_cross_boundary_match(self):
        words = QUOTE.split()
        base, extra = sources(" ".join(words[:5]), " ".join(words[5:]))
        self.assertEqual(analyze_with_supplements(QUOTE, base, extra)["matches"], [])

    def test_primary_evidence_is_retained_when_secondary_changes_boundaries_badly(self):
        base, extra = sources(QUOTE, QUOTE.replace("перед публикацией", "передпубликацией"))
        result = analyze_with_supplements(QUOTE, base, extra)
        tokens = _tokens(QUOTE)
        self.assertTrue(all(any(m["kind"] == "exact" and m["start"] <= t.start and t.end <= m["end"]
                                for m in result["matches"]) for t in tokens))

    def test_no_supplementary_view_is_a_byte_for_byte_legacy_path(self):
        base, _ = sources(QUOTE, QUOTE)
        self.assertEqual(analyze_with_supplements(QUOTE, base), analyze(QUOTE, base))

    def test_saved_secondary_proof_is_revalidated_and_tampering_is_quarantined(self):
        import copy
        base, extra = sources(QUOTE.replace("лабораторного", "лабо раторного"), QUOTE)
        report = analyze_with_supplements(QUOTE, base, extra)
        report.update(text=QUOTE, sources=base + extra, scope="web_search", web={"errors": []})
        case = {"id": "authored", "text_sha256": hashlib.sha256(QUOTE.encode()).hexdigest()}
        checked = _report(report, case)
        self.assertFalse(checked["invalid_highlights"])
        corrupted = copy.deepcopy(report)
        corrupted["matches"][0]["pdf_spacing_evidence"]["primary_span"][1] -= 1
        self.assertEqual(len(_report(corrupted, case)["invalid_highlights"]), 1)
        del report["matches"][0]["pdf_spacing_evidence"]
        self.assertEqual(len(_report(report, case)["invalid_highlights"]), 1)

    def test_web_report_persists_both_views_but_counts_one_page_and_reopens_proof(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        import server
        from plagiarism.storage import Store
        from plagiarism import web_search
        primary = QUOTE.replace("лабораторного", "лабо раторного")
        class Search:
            provider = "authored-fixture"
            def search(self, query, request_id):
                return [{"url": "https://source.test/paper.pdf"}]
        class Pages:
            def fetch(self, url):
                return {"url": url, "text": primary, "content_type": "application/pdf",
                    "raw_pdf_sha256": "a" * 64, "pdf_pages": 1,
                    "supplemental_texts": [{"id": "pdfium-v1", "text": QUOTE,
                        "parser_version": "5.14.0", "extraction": "pdfium-text-v1",
                        "raw_pdf_sha256": "a" * 64, "page_count": 1}]}
        with patch.object(web_search, "selected_provider", return_value="tavily"):
            result = web_search.run(QUOTE, searcher=Search(), fetcher=Pages())
        self.assertEqual(len(result["sources"]), 2)
        self.assertEqual(result["web"]["sources_checked"], 1)
        self.assertEqual(result["stats"]["checked_source_count"], 1)
        self.assertEqual(result["stats"]["matched_words"], len(_tokens(QUOTE)))
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(server.public_report(result)["source_snapshots_count"], 1)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "authored.sqlite3")
            saved = store.save("report", result)
            restored = store.get("report", saved["id"])
            self.assertEqual(restored["matches"], result["matches"])
            self.assertEqual(restored["sources"], result["sources"])
            case = {"id": "authored", "text_sha256": hashlib.sha256(QUOTE.encode()).hexdigest()}
            self.assertFalse(_report(restored, case)["invalid_highlights"])

    def test_bad_or_over_budget_optional_view_cannot_discard_primary_source(self):
        from plagiarism.web_search import _supplemental_source_views
        base, _ = sources(QUOTE, QUOTE)
        source = {**base[0], "title": "authored", "provenance": {}}
        view = {"id": "pdfium-v1", "text": QUOTE, "parser_version": "5.14.0", "extraction": "pdfium-text-v1"}
        for response in ({"content_type": "text/html", "supplemental_texts": [view]},
                         {"content_type": "application/pdf", "supplemental_texts": [{**view, "text_sha256": "changed"}]},
                         {"content_type": "application/pdf", "supplemental_texts": [{**view, "text": "x" * 200000}]}):
            self.assertEqual(_supplemental_source_views(source, response), [])
        self.assertEqual(source["text"], QUOTE)

    def test_alternative_must_describe_the_same_raw_pdf_and_page_count(self):
        from plagiarism.web_search import _supplemental_source_views
        base, _ = sources(QUOTE, QUOTE)
        source = {**base[0], "title": "authored", "provenance": {}}
        view = {"id": "pdfium-v1", "text": QUOTE, "parser_version": "5.14.0", "extraction": "pdfium-text-v1",
                "raw_pdf_sha256": "a" * 64, "page_count": 1}
        response = {"content_type": "application/pdf", "raw_pdf_sha256": "a" * 64,
                    "pdf_pages": 1, "supplemental_texts": [view]}
        self.assertEqual(len(_supplemental_source_views(source, response)), 1)
        for altered in ({**view, "raw_pdf_sha256": "b" * 64}, {**view, "page_count": 2}):
            self.assertEqual(_supplemental_source_views(source, {**response, "supplemental_texts": [altered]}), [])


if __name__ == "__main__":
    unittest.main()
