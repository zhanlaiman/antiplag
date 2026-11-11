from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_unfamiliar_query_probe as probe


class UnfamiliarQueryProbeGuards(unittest.TestCase):
    """Authored temporary files only; no reserved set, provider, or final data."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_snapshot_hash_is_required(self):
        text = "An authored source used only to test the hash guard."
        (self.root / "source.txt").write_text(text)
        self.assertEqual(probe.checked_file(self.root, "source.txt", sha256(text.encode()).hexdigest()), text)
        with self.assertRaisesRegex(ValueError, "hash changed"):
            probe.checked_file(self.root, "source.txt", "0" * 64)

    def test_snapshot_cannot_leave_reserved_root(self):
        with self.assertRaisesRegex(ValueError, "leaves the reserved"):
            probe.checked_file(self.root, "../never-open-this-file.txt", "0" * 64)

    def test_changed_manifest_is_rejected_before_input_reads(self):
        choice = self.root / "choice.json"
        choice.write_text(json.dumps({"query_strategy": probe.web.DENSE_STRATEGY,
            "url_strategy": probe.web.BALANCED_URL_STRATEGY, "matcher_version": probe.MATCHER_VERSION}))
        (self.root / "manifest.json").write_text("This is not the frozen manifest, and is not even JSON.")
        with patch.object(probe, "RESERVED", self.root), patch.object(probe, "CHOICE", choice), \
                patch.object(probe, "checked_file", side_effect=AssertionError("must not read an input")):
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                probe.load_reserved()

    def test_changed_first_use_configuration_is_rejected_before_replanning(self):
        (self.root / "run.json").write_text(json.dumps({"configuration": {"frozen": "old"}}))
        with patch.object(probe, "OUT", self.root), patch.object(probe.web, "plan_queries", side_effect=AssertionError("must not replan")):
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                probe.prepare({}, {}, {}, {"frozen": "new"})

    def test_live_commit_mismatch_stops_before_quota_and_provider(self):
        with patch.object(probe.subprocess, "check_output", return_value="a" * 40), \
                patch.object(probe, "quota", side_effect=AssertionError("must not call quota")):
            with self.assertRaisesRegex(ValueError, "tested Git commit"):
                probe.live({}, {}, {}, {}, {}, "b" * 40)

    def test_offline_scoring_still_requires_the_frozen_manifest(self):
        (self.root / "manifest.json").write_text("{}")
        with patch.object(probe, "RESERVED", self.root):
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                probe.score_live({})


if __name__ == "__main__":
    unittest.main()
