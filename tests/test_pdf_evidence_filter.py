import unittest

from scripts.audit_pdfium_views import filtered_union, valid_new_match, ordered_primary_support


class SupplementaryEvidenceFilterTests(unittest.TestCase):
    def test_noncharacter_rejects_only_supplementary_proof_and_preserves_primary(self):
        first = "Экспериментальная камера сохранила изменение напряжения после повторного охлаждения контрольного образца."
        second = "Библиотекарь записал название книги перед выдачей нового читательского билета."
        supplementary = first.replace("камера сохранила", "камера\ufffe сохранила")
        result = filtered_union(first + "\n\n" + second, second, supplementary)
        self.assertGreater(result["pdfium_unfiltered"]["covered_words"], 0)
        self.assertEqual(result["filtered_union"]["covered_words"], result["plain"]["covered_words"])
        self.assertEqual(len(result["accepted_new_matches"]), 0)
        self.assertEqual(len(result["rejected_new_matches"]), 1)
        self.assertTrue(result["plain_matches_unchanged"])

    def test_noncharacters_and_surrogates_are_rejected_in_either_excerpt(self):
        for value in (0xD800, 0xDFFF, 0xFDD0, 0xFDEF, 0xFFFE, 0xFFFF, 0x1FFFE, 0x10FFFF):
            for field in ("excerpt", "source_excerpt"):
                match = {"excerpt": "Обычный текст", "source_excerpt": "Обычный текст"}
                match[field] += chr(value)
                self.assertFalse(valid_new_match(match), (value, field))
        self.assertTrue(valid_new_match({"excerpt": "Обычный текст — α, й, ё, 2².",
                                        "source_excerpt": "Обычный текст — α, й, ё, 2²."}))

    def test_valid_additional_view_adds_evidence_without_concatenating_sources(self):
        first = "Лаборатория записала показания прибора перед завершением повторного контрольного опыта."
        second = "Сотрудник библиотеки подготовил новые карточки для посетителей читального зала."
        primary = first.replace("контрольного", "кон трольного") + "\n\n" + second
        result = filtered_union(first, primary, first + "\n\n" + second)
        self.assertGreater(result["filtered_union"]["covered_words"], result["plain"]["covered_words"])
        self.assertTrue(result["accepted_new_matches"])
        # Each proof remains a slice of its own source view.
        match = result["accepted_new_matches"][0]
        self.assertEqual(match["source_excerpt"], first.rstrip("."))
        start, end = match["primary_support"]["plain_span"]
        self.assertEqual("".join(primary[start:end].split()), "".join(match["source_excerpt"].split()))

    def test_new_reading_order_cannot_be_presented_as_a_spacing_repair(self):
        target = "Сотрудник лаборатории записал изменения напряжения после завершения повторного контрольного измерения."
        primary = target.replace("изменения напряжения", "напряжения изменения")
        before = filtered_union(target, primary, target, require_order=False)
        after = filtered_union(target, primary, target)
        self.assertGreater(before["additional_covered_words"], 0)
        self.assertEqual(after["additional_covered_words"], 0)
        self.assertEqual(after["rejected_new_matches"][0]["rejection"], "ordered_characters_not_in_primary")

    def test_primary_support_never_maps_punctuation_or_other_characters(self):
        self.assertIsNone(ordered_primary_support("Измерение — повторное", "Измерение - повторное"))
        self.assertIsNone(ordered_primary_support("новое слово", "слово новое"))
        self.assertIsNotNone(ordered_primary_support("контрольного измерения", "кон трольного\nизмерения"))


if __name__ == "__main__":
    unittest.main()
