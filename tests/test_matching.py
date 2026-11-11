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

    def test_larger_similar_match_does_not_remove_exact_evidence_inside_it(self):
        words = [f"term{number:03}" for number in range(20)]
        text = ' '.join(words) + '.'
        changed = words.copy()
        changed[6], changed[14] = 'changed006', 'changed014'
        original = ' '.join(changed) + '.'
        result = analyze(text, [source(original)])
        exact = [match for match in result['matches'] if match['kind'] == 'exact']
        self.assertEqual(len(exact), 2)
        self.assertTrue(any(match['kind'] == 'similar' for match in result['matches']))
        for match in result['matches']:
            self.assertEqual(match['excerpt'], text[match['start']:match['end']])
            self.assertEqual(match['source_excerpt'], original[match['source_start']:match['source_end']])
        self.assertLessEqual(result['stats']['similarity_percent'], 100)

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

    def test_cyrillic_acute_and_grave_stress_are_typographic_variants(self):
        self.assertEqual(normalize("Эконо\u0301мика Пла\u0300новая"), normalize("Экономика Плановая"))
        self.assertEqual(normalize("эконо\u0341мика"), normalize("экономика"))

    def test_stressed_source_matches_plain_text_with_original_end_offsets(self):
        plain = "Плановая экономика изучает распределение ограниченных ресурсов воды"
        accented = "Пла\u0301новая эконо\u0301мика изучает распределение ограниченных ресурсов воды\u0301"
        text = "🧪 Введение. " + plain + "."
        source_text = "🧪 Справка. " + accented + "."
        result = analyze(text, [source(source_text)])
        match = result["matches"][0]
        self.assertEqual(match["kind"], "exact")
        self.assertEqual(match["excerpt"], plain)
        self.assertEqual(match["source_excerpt"], accented)
        self.assertEqual(source_text[match["source_start"]:match["source_end"]], accented)
        self.assertTrue(match["source_excerpt"].endswith("\u0301"))
        self.assertEqual(result["issues"], [])

    def test_trailing_stress_in_checked_text_stays_inside_evidence(self):
        plain = "Плановая экономика изучает распределение ограниченных ресурсов воды"
        text = "🧪 " + plain + "\u0301."
        result = analyze(text, [source(plain + ".")])
        match = result["matches"][0]
        self.assertEqual(match["excerpt"], plain + "\u0301")
        self.assertEqual(text[match["start"]:match["end"]], match["excerpt"])
        self.assertEqual(result["issues"], [])

    def test_breve_is_preserved_and_canonical_forms_match(self):
        self.assertEqual(normalize("район"), normalize("раи\u0306он"))
        self.assertNotEqual(normalize("район"), normalize("раион"))
        plain = "Район использует новые способы распределения ограниченных ресурсов"
        decomposed = plain.replace("й", "и\u0306")
        match = analyze(plain, [source(decomposed)])["matches"][0]
        self.assertEqual(match["kind"], "exact")
        self.assertEqual(match["source_excerpt"], decomposed)

    def test_canonical_latin_accents_and_other_marks_remain_meaningful(self):
        self.assertEqual(normalize("café"), normalize("cafe\u0301"))
        self.assertEqual(normalize("cafè"), normalize("cafe\u0300"))
        self.assertNotEqual(normalize("café"), normalize("cafe"))
        self.assertNotEqual(normalize("q\u0308"), normalize("q"))


if __name__ == "__main__":
    unittest.main()
