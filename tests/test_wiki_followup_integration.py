"""Public run contract with authored replies/pages; no real discovery or I/O."""
from hashlib import sha256
import unittest
from unittest.mock import patch

from plagiarism import web_search as web

TEXT = "Режиссёр обсудил с актёрами расписание новой постановки перед вечерней репетицией."
OLD, NEW = "https://example.org/old", "https://ru.wikipedia.org/wiki/Example"


class Search:
    provider = "tavily"
    def __init__(self, events, name, urls):
        self.events, self.name, self.urls = events, name, urls
        self.provider = "mediawiki" if name == "wiki" else "tavily"
    def search(self, query, request_id):
        self.events.append((self.name, query, request_id))
        return [{"url": url} for url in self.urls]


class Fetch:
    def __init__(self, events):
        self.events = events
    def fetch(self, url):
        self.events.append(("fetch", url))
        return {"url": url, "title": "Открытый источник", "text": TEXT,
                "text_sha256": sha256(TEXT.encode()).hexdigest(), "links": []}


class WikiFollowupIntegrationTests(unittest.TestCase):
    def run_case(self, primary_urls, *, wiki=True):
        events = []
        result = web.run(TEXT, request_id="authored-case", searcher=Search(events, "primary", primary_urls),
            fetcher=Fetch(events), wiki_searcher=Search(events, "wiki", [NEW]), use_wiki_followup=wiki)
        return result, events

    def test_optional_discovery_follows_all_primary_search_and_fetch_work(self):
        before, _ = self.run_case([OLD], wiki=False)
        after, events = self.run_case([OLD])
        primary_fetch_index = events.index(("fetch", OLD))
        wiki_indices = [i for i, event in enumerate(events) if event[0] == "wiki"]
        self.assertTrue(wiki_indices)
        self.assertTrue(all(i > primary_fetch_index for i in wiki_indices))
        self.assertEqual(after["sources"][:len(before["sources"])], before["sources"])
        self.assertEqual(after["web"]["attempted_urls"][:1], [OLD])
        self.assertEqual(after["web"]["primary_query_spans"], before["web"]["query_spans"])
        self.assertEqual(after["stats"]["matched_words"], before["stats"]["matched_words"])
        self.assertTrue(after["web"]["post_wiki_enabled"])
        self.assertLessEqual(after["web"]["queries_attempted"], 8)
        self.assertLessEqual(after["web"]["urls_attempted"], 26)
        for index, (_, _, call_id) in enumerate(event for event in events if event[0] == "wiki"):
            self.assertEqual(call_id, sha256(("authored-case\0mediawiki-post\0" + str(index)).encode()).hexdigest())

    def test_appended_body_resolves_no_sources_failure(self):
        result, events = self.run_case([])
        self.assertEqual(result["stats"]["matched_words"], len(web._tokens(TEXT)))
        self.assertEqual(result["web"]["sources_checked"], 1)
        self.assertFalse(any(e["code"] == "no_sources_checked" for e in result["web"]["errors"]))
        self.assertTrue(all(m["source_url"] == NEW for m in result["matches"]))
        self.assertEqual(result["web"]["status"], "complete")
        self.assertEqual(result["stats"]["uniqueness_percent"], 0)
        self.assertFalse(any(i.get("kind") == "partial_web_check" for i in result["issues"]))
        self.assertTrue(any("двух дополнительных" in note and "двадцати шести" in note
                            for note in result["web"]["limitations"]))

    def test_injected_primary_io_never_constructs_implicit_real_secondary(self):
        with patch.object(web, "DEFAULT_WIKI_FOLLOWUP", True), \
                patch("plagiarism.search_mediawiki.MediaWikiSearch", side_effect=AssertionError("network constructor forbidden")):
            events = []
            report = web.run(TEXT, searcher=Search(events, "primary", []), fetcher=Fetch(events))
        self.assertFalse(report["web"]["post_wiki_enabled"])
        self.assertFalse(any(event[0] == "wiki" for event in events))

    def test_invalid_option_fails_before_primary_search(self):
        events = []
        with self.assertRaises(ValueError):
            web.run(TEXT, searcher=Search(events, "primary", []), fetcher=Fetch(events), use_wiki_followup="yes")
        self.assertEqual(events, [])

    def test_public_status_declares_all_provider_and_page_limits(self):
        with patch.dict("os.environ", {"ISTOK_WEB_WIKI_FOLLOWUP": "1", "ISTOK_WEB_ZERO_FALLBACK": "1"}):
            status = web.status()
        self.assertEqual(status["max_queries"], 8)
        self.assertEqual(status["max_primary_queries"], 6)
        self.assertEqual(status["max_secondary_queries"], 2)
        self.assertEqual(status["max_urls"], 26)
        self.assertEqual(status["secondary_provider"], "mediawiki")

    def test_normal_default_run_constructs_and_uses_secondary_adapter(self):
        events = []
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(web, "DEFAULT_WIKI_FOLLOWUP", True), \
                patch.object(web, "selected_provider", return_value="tavily"), \
                patch.object(web, "SearchBudget", return_value=object()), \
                patch("plagiarism.search_tavily.TavilySearch", return_value=Search(events, "primary", [])), \
                patch("plagiarism.web_fetch.Fetcher", return_value=Fetch(events)), \
                patch("plagiarism.search_mediawiki.MediaWikiSearch", return_value=Search(events, "wiki", [NEW])):
            report = web.run(TEXT, request_id="public-default")
        self.assertTrue(report["web"]["post_wiki_enabled"])
        self.assertEqual(report["web"]["query_strategy"], web.PARAGRAPH_STRATEGY)
        self.assertTrue(any(event[0] == "wiki" for event in events))
        self.assertEqual(report["web"]["sources_checked"], 1)

    def test_previous_complete_hundred_percent_is_recomputed_after_new_match(self):
        class SelectiveFetch(Fetch):
            def fetch(self, url):
                page = super().fetch(url)
                if url == OLD:
                    page["text"] = "Фестиваль завершился обсуждением билетов. Гардеробщица спокойно закрыла служебный вход."
                    page["text_sha256"] = sha256(page["text"].encode()).hexdigest()
                return page
        reports = []
        for enabled in (False, True):
            events = []
            reports.append(web.run(TEXT, request_id="fresh-found", searcher=Search(events, "primary", [OLD]),
                fetcher=SelectiveFetch(events), wiki_searcher=Search(events, "wiki", [NEW]), use_wiki_followup=enabled))
        self.assertEqual(reports[0]["stats"]["uniqueness_percent"], 100)
        self.assertEqual(reports[1]["web"]["status"], "complete")
        self.assertEqual(reports[1]["stats"]["uniqueness_percent"], 0)


if __name__ == "__main__":
    unittest.main()
