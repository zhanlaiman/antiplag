import unittest
from urllib.parse import unquote, urlsplit

from plagiarism.input_references import MAX_NAMED_REFERENCES, extract_named_references
from plagiarism.matching import MAX_TEXT_CHARS


PASSAGE = '«Точные измерения помогают проверить качество работы школьного датчика температуры» '


class InputReferenceTests(unittest.TestCase):
    def test_proven_syntax_negations_and_unverified_false_citation(self):
        cases = [
            ("other_article", '(русская Википедия, статья «Шахматные часы»).', "Шахматные_часы"),
            ("absent_source_marker", '(учебник, статья «Метрология»).', None),
            ("absent_explicit_article", '(русская Википедия, «Метрология»).', None),
            ("explicit_negation", '(не русская Википедия, статья «Метрология»).', None),
            ("negated_article", '(Википедия, не статья «Метрология»).', None),
            ("english_marker", '(Russian Wikipedia, article “Metrology”).', "Metrology"),
            ("false_attribution", '(русская Википедия, статья «Метрология»).', "Метрология"),
            ("path_injection", '(Википедия, статья «https://example.test/a?x=1#f»).', "https://example.test/a?x=1#f"),
        ]
        for name, citation, expected_path in cases:
            with self.subTest(name=name):
                text = ("«Зелёный термометр выдаёт квадратные булочки вместо температуры» "
                        if name == "false_attribution" else PASSAGE) + citation
                result = extract_named_references(text)
                if expected_path is None:
                    self.assertEqual(result, [])
                    continue
                self.assertEqual(len(result), 1)
                reference = result[0]
                parsed = urlsplit(reference["url"])
                self.assertEqual((parsed.scheme, parsed.netloc, parsed.query, parsed.fragment),
                                 ("https", "ru.wikipedia.org", "", ""))
                self.assertEqual(unquote(parsed.path.removeprefix("/wiki/")), expected_path)
                self.assertEqual(reference["source_claim"], "unverified_input_reference")

    def test_offsets_are_original_python_characters_and_only_hint_metadata_is_returned(self):
        text = '😀 Введение. ' + PASSAGE + '(русская Википедия, статья «Шахматные   часы»).'
        reference = extract_named_references(text)[0]
        self.assertEqual(set(reference), {"url", "title", "citation_span", "title_span", "source_claim"})
        self.assertEqual(text[slice(*reference["citation_span"])], '(русская Википедия, статья «Шахматные   часы»)')
        self.assertEqual(text[slice(*reference["title_span"])], 'Шахматные   часы')
        self.assertEqual(reference["title"], "Шахматные часы")
        self.assertTrue(reference["url"].endswith('%D0%A8%D0%B0%D1%85%D0%BC%D0%B0%D1%82%D0%BD%D1%8B%D0%B5_%D1%87%D0%B0%D1%81%D1%8B'))

    def test_dedup_preserves_first_original_citation_and_title_spans(self):
        first = '(Википедия, статья «Шахматные часы»)'
        text = first + ' Затем ' + '(русская Википедия, статье «Шахматные   часы»)'
        result = extract_named_references(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["citation_span"], [0, len(first)])
        self.assertEqual(text[slice(*result[0]["title_span"])], "Шахматные часы")

    def test_six_reference_cap_preserves_input_order(self):
        text = " ".join(f'(Википедия, статья «Статья {index}»)' for index in range(10))
        result = extract_named_references(text)
        self.assertEqual(MAX_NAMED_REFERENCES, 6)
        self.assertEqual([item["title"] for item in result], [f"Статья {index}" for index in range(6)])

    def test_duplicate_references_do_not_consume_the_six_reference_cap(self):
        repeated = '(Википедия, статья «Повтор»). ' * 20
        text = repeated + " ".join(f'(Википедия, статья «Статья {index}»)' for index in range(8))
        result = extract_named_references(text)
        self.assertEqual([item["title"] for item in result], ["Повтор"] + [f"Статья {index}" for index in range(5)])

    def test_negation_inside_an_article_title_is_not_a_negated_citation(self):
        result = extract_named_references('(Википедия, статья «Не навреди»)')
        self.assertEqual([item["title"] for item in result], ["Не навреди"])

    def test_no_expansion_to_other_publishers_or_non_parenthetical_citations(self):
        for text in ('Статья «Метрология» русской Википедии.',
                     '(Wikipedia, article “Metrology”)',
                     '(Britannica, article “Metrology”)',
                     '(Википедии, статья «Метрология»)',
                     '(Википедия, название «Метрология»)'):
            with self.subTest(text=text):
                self.assertEqual(extract_named_references(text), [])

    def test_invalid_title_characters_do_not_create_urls(self):
        for title in ('Метрология\u200b', 'Метрология\x00', '\ud800', '..'):
            with self.subTest(title=repr(title)):
                self.assertEqual(extract_named_references(f'(Википедия, статья «{title}»)'), [])

    def test_bounds_and_empty_input(self):
        self.assertEqual(extract_named_references(""), [])
        self.assertEqual(extract_named_references("a" * MAX_TEXT_CHARS), [])
        for text in (None, 123, "a" * (MAX_TEXT_CHARS + 1)):
            with self.subTest(kind=type(text).__name__):
                with self.assertRaises(ValueError):
                    extract_named_references(text)

    def test_many_unclosed_parentheticals_have_no_preceding_quote_rescans(self):
        # Repeated opening brackets and quotes formerly made attaching every
        # preceding quote an unnecessary quadratic operation in the prototype.
        text = ('«слова» (' * 15000) + '(Википедия, статья «Метрология»)'
        result = extract_named_references(text)
        self.assertEqual([item["title"] for item in result], ["Метрология"])


if __name__ == "__main__":
    unittest.main()
