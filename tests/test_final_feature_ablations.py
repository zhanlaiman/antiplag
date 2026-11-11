"""Identity guards using authored payloads only; no evaluation materials read."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scripts.replay_final_feature_ablations as replay


PDF_URL = "https://example.org/owned.pdf"
PRIMARY = "Авторский пример содержит достаточно длинный первичный текст для проверки идентичности сохранённого источника."
SUPPLEMENT = "Авторский пример содержит достаточно длинный дополнительный текст для проверки идентичности сохранённого источника."
NAMED_SOURCE = "This independently authored source body is used only for synthetic identity tests."
PROVEN_NAMED_DIGEST = "9c889daf3404df5aa9555e89ef5687cc6e07cd914eb1f290a0d6cbbde3e91659"


def digest(text):
    return sha256(text.encode()).hexdigest()


def owned_pdf():
    return {"url": PDF_URL, "requested_url": PDF_URL, "title": "Authored PDF fixture",
            "text": PRIMARY, "text_sha256": digest(PRIMARY), "links": [],
            "content_type": "application/pdf", "raw_pdf_sha256": "a" * 64, "pdf_pages": 1,
            "supplemental_texts": [{"id": "pdfium-v1", "extraction": "pdfium-text-v1",
                "parser_version": "5.14.0", "raw_pdf_sha256": "a" * 64, "page_count": 1,
                "text": SUPPLEMENT, "text_sha256": digest(SUPPLEMENT)}],
            "optional_status": {"engine": "pdfium", "status": "complete", "code": None}}


class FinalFeatureAblationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def pages(self, page, saved, enabled=True):
        return replay.FeaturePages({PDF_URL: deepcopy(page)}, {PDF_URL},
            cache_directory=self.root / "empty-page-cache", fill_missing=False,
            refresh_missing_links=False, pdf_payloads={PDF_URL: saved}, enable_pdf=enabled)

    def test_view_attachment_requires_identical_primary_even_with_a_new_valid_hash(self):
        saved = owned_pdf()
        self.assertEqual(replay.validate_pdf_payload(saved), PDF_URL)
        valid = self.pages(saved, saved)
        self.assertEqual(valid.fetch(PDF_URL)["supplemental_texts"], saved["supplemental_texts"])
        self.assertEqual(len(valid.applied_views), 1)
        changed = deepcopy(saved)
        changed["text"] = PRIMARY + " Изменённая новая редакция."
        changed["text_sha256"] = digest(changed["text"])
        pages = self.pages(changed, saved)
        with self.assertRaises(ValueError):
            pages.fetch(PDF_URL)
        self.assertEqual(pages.applied_views, [])
        self.assertEqual(pages.validation_errors, [PDF_URL])
        self.assertEqual(pages.fresh_attempts, 0)

    def test_mutated_supplement_and_raw_identity_are_rejected_at_attachment(self):
        for mutation in ("supplement_text", "primary_text", "raw_digest", "view_raw_digest"):
            with self.subTest(mutation=mutation):
                saved = owned_pdf()
                if mutation == "supplement_text":
                    saved["supplemental_texts"][0]["text"] += " Unrecorded change."
                elif mutation == "primary_text":
                    saved["text"] += " Unrecorded change."
                elif mutation == "raw_digest":
                    saved["raw_pdf_sha256"] = "b" * 64
                else:
                    saved["supplemental_texts"][0]["raw_pdf_sha256"] = "b" * 64
                with self.assertRaises(ValueError):
                    replay.validate_pdf_payload(saved)
                pages = self.pages(owned_pdf(), saved)
                with self.assertRaises(ValueError):
                    pages.fetch(PDF_URL)
                self.assertEqual(pages.applied_views, [])
                self.assertEqual(pages.validation_errors, [PDF_URL])
                self.assertEqual(pages.fresh_attempts, 0)

    def test_primary_only_ablation_strips_a_cached_view_without_mutating_the_cache(self):
        cached = owned_pdf()
        pages = self.pages(cached, cached, enabled=False)
        primary = pages.fetch(PDF_URL)
        self.assertEqual(primary["text"], PRIMARY)
        self.assertNotIn("supplemental_texts", primary)
        self.assertNotIn("optional_status", primary)
        self.assertEqual(pages.applied_views, [])
        self.assertIn("supplemental_texts", pages.pool[PDF_URL])
        self.assertIn("optional_status", pages.pool[PDF_URL])
        self.assertEqual(pages.fresh_attempts, 0)

    def test_named_payload_is_bound_to_actual_submitted_text_and_saved_input_identity(self):
        text = 'Авторский пример. (русская Википедия, статья «Метрология»).'
        hint = replay.extract_named_references(text)[0]
        case = {"id": "D14", "text_sha256": digest(text)}
        named = self.root / "named"
        named.mkdir()
        saved = {"state": "complete", "fetch_invocations": 1, "input_text_sha256": digest(text),
                 "requested_url": hint["url"], "response": {"url": hint["url"], "title": "Authored source",
                     "text": NAMED_SOURCE, "text_sha256": digest(NAMED_SOURCE), "links": []}}
        path = named / "D14-fetch-response.json"
        # The replay intentionally pins one real source digest. Alias only our
        # authored source sentinel to that value so this test can exercise the
        # independent input guard without opening the real response or labels.
        source_alias = lambda value: PROVEN_NAMED_DIGEST if value == NAMED_SOURCE else digest(value)
        with patch.object(replay, "ROOT", self.root), patch.object(replay, "NAMED_DIR", named), \
                patch.object(replay, "text_digest", side_effect=source_alias):
            path.write_text(json.dumps(saved))
            page, _ = replay.load_named_payload(case, text)
            self.assertEqual(page["text"], NAMED_SOURCE)
            with self.assertRaises(ValueError):
                replay.load_named_payload(case, "Изменённое тело. " + text)
            for field, replacement in (("input_text_sha256", "c" * 64),
                                       ("requested_url", "https://ru.wikipedia.org/wiki/Other")):
                with self.subTest(field=field):
                    changed = deepcopy(saved)
                    changed[field] = replacement
                    path.write_text(json.dumps(changed))
                    with self.assertRaises(ValueError):
                        replay.load_named_payload(case, text)


if __name__ == "__main__":
    unittest.main()
