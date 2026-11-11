"""Count expensive repeated work without relying on wall-clock timing."""
from collections import Counter
import unittest
from unittest.mock import patch

from plagiarism import matching


class AnchorTokenizationTests(unittest.TestCase):
    def test_full_exact_excerpt_is_tokenized_once_regardless_of_sentence_count(self):
        sentence = "Researchers carefully inspected the detailed measurements before publishing their final observations.\n"
        for count in (20, 80):
            with self.subTest(sentences=count):
                text = sentence * count
                original, calls = matching._tokens, Counter()
                def counted(value):
                    calls[value] += 1
                    return original(value)
                with patch.object(matching, "_tokens", side_effect=counted):
                    result = matching.analyze(text, [{"id": "owned", "text": text}])
                exact = [m for m in result["matches"] if m["kind"] == "exact"]
                self.assertEqual(len(exact), 1)
                self.assertEqual(exact[0]["excerpt"], text[:-2])
                self.assertEqual(calls[exact[0]["excerpt"]], 1)


if __name__ == "__main__":
    unittest.main()
