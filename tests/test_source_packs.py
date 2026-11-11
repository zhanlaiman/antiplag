from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from plagiarism import source_packs as packs


class SourcePackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.text = "  Исходный текст тестового ответа API.\n\nВторой абзац сохраняет пробелы и пунктуацию.  "
        self.page = {"pageid": 123, "title": "Экономическая теория", "canonicalurl": "https://ru.wikipedia.org/wiki/Экономическая_теория",
            "extract": self.text, "revisions": [{"revid": 456, "timestamp": "2026-11-11T12:00:00Z"}]}

    def response(self, parameters):
        return {"query": {"pages": [deepcopy(self.page)]}}

    def make_pack(self):
        return packs.collect_pack(["Экономическая теория"], fetch_json=self.response, pause_seconds=0)

    def test_extract_body_preserved_and_provenance_reports_revision_binding(self):
        fetch = Mock(side_effect=self.response)
        source = packs.fetch_article("Экономическая теория", fetch)
        self.assertEqual(source["text"], self.text)
        self.assertEqual(source["id"], "wiki-ru-123-456")
        self.assertEqual(source["provenance"]["text_sha256"], sha256(self.text.encode()).hexdigest())
        self.assertEqual(source["provenance"]["revision_binding"], "reported_latest_revision")
        self.assertEqual(source["provenance"]["license_name"], "CC BY-SA 4.0")
        self.assertIn("action=history", source["provenance"]["attribution"])
        self.assertEqual(fetch.call_args.args[0]["explaintext"], "1")
        self.assertNotIn("exchars", fetch.call_args.args[0])

    def test_redirects_to_same_body_are_deduplicated_and_disclosed(self):
        pack = packs.collect_pack(["Экономическая теория", "Другой заголовок"], fetch_json=self.response, pause_seconds=0)
        self.assertEqual(pack["source_count"], 1)
        self.assertEqual(pack["requested_source_count"], 2)
        self.assertEqual(len(pack["omitted"]), 1)
        self.assertIn("повтор исключён", pack["omitted"][0]["error"])

    def test_same_body_with_spacing_changes_is_still_duplicate(self):
        calls = 0

        def response(parameters):
            nonlocal calls
            calls += 1
            page = deepcopy(self.page)
            if calls == 2:
                page.update(pageid=124, extract=" ".join(self.text.split()))
            return {"query": {"pages": [page]}}

        pack = packs.collect_pack(["Первый", "Второй"], fetch_json=response, pause_seconds=0)
        self.assertEqual(pack["source_count"], 1)
        self.assertEqual(len(pack["omitted"]), 1)

    def test_missing_article_and_overlong_article_are_not_silently_included(self):
        def response(parameters):
            if parameters["titles"] == "Отсутствует":
                return {"query": {"pages": [{"title": "Отсутствует", "missing": True}]}}
            page = deepcopy(self.page)
            if parameters["titles"] == "Длинная":
                page["extract"] = "Я" * (packs.MAX_TEXT_CHARS + 1)
            return {"query": {"pages": [page]}}

        pack = packs.collect_pack(["Отсутствует", "Длинная", "Экономическая теория"], fetch_json=response, pause_seconds=0)
        self.assertEqual(pack["source_count"], 1)
        self.assertEqual(len(pack["omitted"]), 2)
        self.assertEqual(pack["sources"][0]["text"], self.text)

    def test_empty_collection_cannot_replace_saved_pack(self):
        response = lambda parameters: {"query": {"pages": [{"title": "Missing", "missing": True}]}}
        with self.assertRaisesRegex(packs.SourcePackError, "ни одного источника"):
            packs.collect_pack(["Missing"], fetch_json=response, pause_seconds=0)

    def test_listing_reports_actual_collection_size_and_omissions(self):
        pack = packs.collect_pack(["Первый", "Повтор"], fetch_json=self.response, pause_seconds=0)
        (self.directory / (pack["id"] + ".json")).write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        listing = packs.list_packs(self.directory)
        self.assertEqual(listing[0]["source_count"], 1)
        self.assertEqual(listing[0]["requested_source_count"], 2)
        self.assertEqual(listing[0]["total_characters"], len(self.text))
        self.assertEqual(len(listing[0]["omitted"]), 1)
        self.assertEqual(packs.load_pack(pack["id"], self.directory), pack)

    def test_invalid_provenance_or_modified_body_is_rejected(self):
        mutations = [
            lambda source: source.pop("provenance"),
            lambda source: source["provenance"].update(license_url="https://example.org/license"),
            lambda source: source["provenance"].update(history_url="https://external.example/history"),
            lambda source: source["provenance"].update(revision_id=999),
            lambda source: source["provenance"].update(revision_url="https://ru.wikipedia.org/w/index.php?oldid=999"),
            lambda source: source["provenance"].update(attribution=""),
            lambda source: source["provenance"].update(fetched_at="2026-11-11"),
            lambda source: source.update(text=source["text"] + " Изменение"),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(mutation=number):
                pack = self.make_pack()
                mutate(pack["sources"][0])
                with self.assertRaises(packs.SourcePackError):
                    packs.validate_pack(pack)

    def test_pack_identity_prevents_path_traversal(self):
        for identifier in ("../private", "/tmp/private", "economics/ru", "", 123):
            with self.subTest(identifier=identifier):
                with self.assertRaises(packs.SourcePackError):
                    packs.load_pack(identifier, self.directory)

    def test_http_request_identifies_application(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"query": {"pages": []}}).encode()
        with patch.object(packs, "urlopen", return_value=response) as urlopen:
            packs.request_json({"action": "query", "titles": "Экономика"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), packs.USER_AGENT)
        self.assertTrue(request.full_url.startswith(packs.API_URL + "?"))


if __name__ == "__main__":
    unittest.main()
