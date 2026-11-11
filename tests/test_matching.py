import unittest

from plagiarism.matching import analyze, normalize


ORIGINAL = "Исследователи обнаружили необычные изменения температуры воды в северной части большого озера."


def source(text=ORIGINAL, identifier="one"):
    return {"id": identifier, "title": "Исследование озера", "url": "https://example.org/research", "text": text}


class MatchingTests(unittest.TestCase):
    def test_exact_evidence_preserves_original_offsets(self):
        text = "🧪 Введение. " + ORIGINAL + " Следующий абзац."
        result = analyze(text, [source()])
        match = result["matches"][0]
        self.assertEqual(match["excerpt"], text[match["start"]:match["end"]])
        self.assertEqual(match["source_excerpt"], ORIGINAL[match["source_start"]:match["source_end"]])
        self.assertEqual(match["kind"], "exact")

    def test_same_topic_is_not_enough(self):
        text = "Учёные изучили нагрев озёрной воды и связали наблюдения с погодой в регионе."
        self.assertEqual(analyze(text, [source()])["matches"], [])

    def test_zero_width_and_mixed_script_substitutions(self):
        text = ORIGINAL.replace("обнаружили", "oбн\u200bаружили")
        result = analyze(text, [source()])
        self.assertTrue(result["matches"])
        self.assertEqual({issue["kind"] for issue in result["issues"]}, {"mixed_scripts", "invisible_characters"})
        self.assertEqual(normalize(text), normalize(ORIGINAL))

    def test_duplicate_sources_count_coverage_once(self):
        result = analyze(ORIGINAL, [source(identifier="one"), source(identifier="two")])
        self.assertEqual(result["stats"]["source_count"], 2)
        self.assertLessEqual(result["stats"]["similarity_percent"], 100)
        self.assertEqual(result["stats"]["matched_characters"], len(ORIGINAL.rstrip(".")))

    def test_small_partial_edit_is_a_review_candidate(self):
        text = ORIGINAL.replace("необычные", "заметные").replace("северной", "южной")
        result = analyze(text, [source()])
        self.assertTrue(any(match["kind"] == "similar" for match in result["matches"]))

    def test_quotes_are_preserved_and_marked(self):
        match = analyze("Автор пишет «" + ORIGINAL + "».", [source()])["matches"][0]
        self.assertTrue(match["quoted"])

    def test_short_generic_wording_is_ignored(self):
        self.assertEqual(analyze("Таким образом можно сделать вывод.", [source("Таким образом можно сделать вывод.")])["matches"], [])

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            analyze("  ", [])

    def test_no_sources_returns_zero_without_originality_claim(self):
        result = analyze(ORIGINAL, [])
        self.assertEqual(result["stats"]["checked_source_count"], 0)
        self.assertEqual(result["matches"], [])


if __name__ == "__main__":
    unittest.main()
