"""Independent-topic and genuine-compound controls for line-wrap normalization."""
import unittest

from plagiarism.matching import analyze, literal_equivalent


class TypographyControls(unittest.TestCase):
    def test_word_wrap_matches_either_side_and_keeps_original_evidence(self):
        original = "The telescope collected detailed measurements of extremely distant galaxies before sunrise."
        wrapped = original.replace("extremely", "ex-\ntremely")
        other_wrap = original.replace("telescope", "tele-\nscope")
        for target, other in ((original, wrapped), (wrapped, original), (other_wrap, wrapped)):
            with self.subTest(target=target):
                matches = analyze(target, [{"id": "wrapped", "text": other}])["matches"]
                full = [m for m in matches if m["kind"] == "exact" and m["start"] == 0 and m["end"] == len(target) - 1]
                self.assertEqual(len(full), 1)
                match = full[0]
                self.assertEqual(match["excerpt"], target[match["start"]:match["end"]])
                self.assertEqual(match["source_excerpt"], other[match["source_start"]:match["source_end"]])
                self.assertTrue(literal_equivalent(match["excerpt"], match["source_excerpt"]))

    def test_only_one_line_break_between_letters_is_a_join_alternative(self):
        self.assertTrue(literal_equivalent("extremely", "ex- \r\n  tremely"))
        for broken in ("ex-\n\ntremely", "ex-\n \ntremely", "ex tremely", "ex- tremely", "ex--\ntremely"):
            with self.subTest(broken=broken):
                self.assertFalse(literal_equivalent("extremely", broken))
        self.assertFalse(literal_equivalent("sample12", "sample1-\n2"))
        self.assertFalse(literal_equivalent("величины", "ве личины"))
        self.assertFalse(literal_equivalent("", ""))

    def test_independent_same_topic_pairs_do_not_match_after_word_wraps(self):
        pairs = [
            ("The tele-\nscope collected detailed images of a distant star before sunrise.",
             "An observatory can measure the brightness of astronomical objects using calibrated sensors."),
            ("Coastal obser-\nvations help a harbour pilot estimate the depth near a shallow channel.",
             "A tide gauge records changes in sea level at a fixed place beside the shore."),
            ("The photo-\ngrapher adjusted the lens before taking a portrait beside the window.",
             "Exposure time and aperture influence how a camera records available light."),
            ("The pro-\ngram keeps a small list of borrowed books in a local file.",
             "A database transaction groups related updates so that they succeed or fail together."),
            ("The ther-\nmometer was moved into the shade after the morning inspection.",
             "Measurement uncertainty expresses a range associated with an observed quantity."),
            ("The astro-\nnomy club prepared a projection screen for the solar event.",
             "Direct viewing of the Sun requires suitable protection for the observer's eyes."),
        ]
        for target, other in pairs:
            with self.subTest(target=target):
                self.assertEqual(analyze(target, [{"id": "independent", "text": other}])["matches"], [])

    def test_genuine_compound_at_line_end_keeps_its_original_match(self):
        target = "The project measured long-term changes in ocean temperatures during several seasons."
        other = "The project measured long-\nterm changes in ocean temperatures during several seasons."
        matches = analyze(target, [{"id": "compound", "text": other}])["matches"]
        self.assertTrue(any(m["kind"] == "exact" and m["start"] == 0
                            and m["end"] == len(target) - 1 for m in matches))


if __name__ == "__main__":
    unittest.main()
