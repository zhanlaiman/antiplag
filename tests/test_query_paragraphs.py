"""Independent authored plans; no web replies, corpus, private inputs or models."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from plagiarism.matching import _tokens
from plagiarism.query_paragraphs import PARAGRAPH_STRATEGY, paragraph_redundancy_queries


A = "amber bravo cedar delta ember frost globe hazel ivory jasper".split()
B = A[:-1] + ["kelvin"]
C = "lunar maple north ocean pearl quartz raven solar tulip umber".split()
SHORT = "winter yellow zephyr acorn birch coral dawn elm fern garnet".split()
SECOND = "silver bronze copper nickel iron cobalt marble granite slate basalt".split()


def fixture(windows=(A, B, C), target=SHORT, *, separator="\n\n"):
    long = [word for words in windows for word in words] + ["violet"] * 20
    text = " ".join(long) + separator + " ".join(target)
    queries, cursor = [], 0
    for words in windows:
        phrase = " ".join(words)
        start = text.index(phrase, cursor)
        queries.append({"query": '"' + phrase.lower() + '"', "start": start, "end": start + len(phrase)})
        cursor = start + len(phrase)
    return text, queries


class ParagraphQueryTests(unittest.TestCase):
    def test_replaces_redundant_tie_at_later_index_retains_distinctive_later_query(self):
        text, planned = fixture()
        result = paragraph_redundancy_queries(text, planned, ())
        self.assertEqual(PARAGRAPH_STRATEGY, "hybrid-citation-v5-paragraph-redundancy")
        self.assertEqual(len(result), 3)  # limit6 still applies to an eligible actual short plan.
        self.assertEqual(result[0], planned[0])
        self.assertEqual(result[2], planned[2])
        self.assertEqual(result[1]["query"], '"' + " ".join(SHORT) + '"')
        self.assertEqual([i for i, (a, b) in enumerate(zip(planned, result)) if a != b], [1])
        self.assertEqual(text[result[1]["start"]:result[1]["end"]], " ".join(SHORT))

    def test_citation_string_priority_chooses_other_redundant_query(self):
        text, planned = fixture()
        protected = (query for query in [planned[1]["query"]])
        result = paragraph_redundancy_queries(text, planned, protected)
        self.assertEqual(result[1:], planned[1:])
        self.assertEqual(result[0]["query"], '"' + " ".join(SHORT) + '"')

    def test_all_protected_queries_remain_unchanged(self):
        text, planned = fixture()
        self.assertEqual(paragraph_redundancy_queries(text, planned, {q["query"] for q in planned}), planned)

    def test_smaller_requested_limit_keeps_supplied_short_plan_without_trim_or_refill(self):
        text, planned = fixture()
        for limit in (1, 3, 5):
            with self.subTest(limit=limit):
                result = paragraph_redundancy_queries(text, planned, (), limit=limit)
                self.assertEqual(result, planned)
                self.assertEqual(len(result), 3)
        self.assertNotEqual(paragraph_redundancy_queries(text, planned, (), limit=6), planned)

    def test_no_uncovered_short_paragraph_or_only_nine_words_keeps_plan(self):
        text, planned = fixture()
        self.assertEqual(paragraph_redundancy_queries(text.split("\n\n")[0], planned, ()), planned)
        short_text, planned = fixture(target=SHORT[:9])
        self.assertEqual(paragraph_redundancy_queries(short_text, planned, ()), planned)

    def test_duplicate_existing_phrase_in_another_paragraph_is_not_added(self):
        text, planned = fixture(windows=(SHORT, A, B), target=SHORT)
        self.assertEqual(paragraph_redundancy_queries(text, planned, ()), planned)

    def test_earliest_uncovered_target_wins_and_existing_covered_target_is_skipped(self):
        text, planned = fixture()
        text += "\n\n" + " ".join(SECOND)
        result = paragraph_redundancy_queries(text, planned, ())
        self.assertEqual(result[1]["query"], '"' + " ".join(SHORT) + '"')
        start = text.index(" ".join(SHORT))
        covered = {"query": '"' + " ".join(SHORT) + '"', "start": start, "end": start + len(" ".join(SHORT))}
        planned.append(covered)
        result = paragraph_redundancy_queries(text, planned, ())
        self.assertEqual(result[1]["query"], '"' + " ".join(SECOND) + '"')
        self.assertEqual(result[3], covered)
        self.assertEqual(len(result), len(planned))

    def test_forty_word_target_is_eligible_but_query_uses_first_ten_only(self):
        words = SHORT + ["clover"] * 30
        text, planned = fixture(target=words, separator="\n \t\r\n")
        result = paragraph_redundancy_queries(text, planned, ())
        self.assertEqual(result[1]["query"], '"' + " ".join(SHORT) + '"')
        start = text.index(" ".join(SHORT))
        self.assertEqual((result[1]["start"], result[1]["end"]), (start, start + len(" ".join(SHORT))))
        text, planned = fixture(target=SHORT + ["clover"] * 31)
        self.assertEqual(paragraph_redundancy_queries(text, planned, ()), planned)

    def test_query_normalizes_case_caps_each_token_and_preserves_raw_source_end(self):
        words = ["W" * 45] + [word.upper() for word in SHORT[1:]]
        text, planned = fixture(target=words)
        result = paragraph_redundancy_queries(text, planned, ())
        self.assertEqual(result[1]["query"], '"' + " ".join(["w" * 40] + SHORT[1:]) + '"')
        self.assertEqual(text[result[1]["start"]:result[1]["end"]], " ".join(words))
        oversized, planned = fixture(target=[letter * 40 for letter in "abcdefghij"])
        self.assertEqual(paragraph_redundancy_queries(oversized, planned, ()), planned)

    def test_redundancy_ignores_short_tokens_and_decimal_words_and_uses_directional_denominator(self):
        short_common = "a be by in to we no on 2024".split()
        a = short_common + ["granite"]
        b = "granite orchard meadow pasture forest valley harbor island marshes ridge".split()
        c = short_common + ["basalt"]
        text, planned = fixture(windows=(a, b, c))
        result = paragraph_redundancy_queries(text, planned, ())
        # {granite} is entirely covered by q1; q2 shares only ignored tokens.
        self.assertEqual(result[0]["query"], '"' + " ".join(SHORT) + '"')
        self.assertEqual(result[1:], planned[1:])

    def test_replacement_requires_two_queries_in_same_more_than_forty_word_paragraph(self):
        first = " ".join(A + ["lavender"] * 40)
        second = " ".join(B + ["marigold"] * 40)
        text = first + "\n\n" + second + "\n\n" + " ".join(SHORT)
        planned = [{"query": '"' + " ".join(A) + '"', "start": 0, "end": len(" ".join(A))},
            {"query": '"' + " ".join(B) + '"', "start": len(first) + 2, "end": len(first) + 2 + len(" ".join(B))}]
        self.assertEqual(paragraph_redundancy_queries(text, planned, ()), planned)
        text, planned = fixture()
        long, short = text.split("\n\n")
        forty = " ".join(long.split()[:40]) + "\n\n" + short
        self.assertEqual(paragraph_redundancy_queries(forty, planned, ()), planned)

    def test_input_dictionary_and_nested_metadata_are_not_mutated_and_no_planner_network_is_used(self):
        text, planned = fixture(); planned[0]["metadata"] = {"notes": ["authored"]}; original = deepcopy(planned)
        with patch("plagiarism.web_search.plan_queries", side_effect=AssertionError("Use supplied plan only")), \
                patch("socket.socket", side_effect=AssertionError("Pure query transformation has no network")):
            result = paragraph_redundancy_queries(text, planned, ())
        self.assertEqual(planned, original)
        result[0]["metadata"]["notes"].append("changed-output")
        self.assertEqual(planned, original)


if __name__ == "__main__":
    unittest.main()
