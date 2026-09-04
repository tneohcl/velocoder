"""Regression tests for presets.py (preset persistence).

Run with:  python3 -m unittest discover -s tests -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import presets  # noqa: E402


class TestPresets(unittest.TestCase):
    """presets.json is the single file the app actually reads/writes --
    built-ins first, any user-saved preset appended after them. A fake,
    isolated builtin_presets.json (not the real shipped one) is what these
    tests reseed from, so the expected content is under the test's own
    control rather than coupled to whatever the real 9 built-ins are."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        self.path = self.tmpdir / "presets.json"
        self.builtin_path = self.tmpdir / "builtin_presets.json"
        self.builtin_presets = [{"name": "Builtin One"}, {"name": "Builtin Two"}]
        self.builtin_path.write_text(json.dumps({"presets": self.builtin_presets}))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_seeds_from_builtins(self):
        loaded = presets.load_presets(self.path, self.builtin_path)
        self.assertEqual(loaded, self.builtin_presets)

    def test_missing_file_writes_the_seeded_file_to_disk(self):
        # Not just returned in memory -- the whole point of "seed on first
        # run" is that a future Save As appends to a file already sitting
        # on disk, not one that only gets created on that first write.
        presets.load_presets(self.path, self.builtin_path)
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text())["presets"], self.builtin_presets)

    def test_save_then_load_round_trips(self):
        data = self.builtin_presets + [{"name": "My Preset", "encoder": "hevc_vaapi", "quality_value": 30}]
        presets.save_presets(data, self.path)
        self.assertEqual(presets.load_presets(self.path, self.builtin_path), data)

    def test_save_overwrites_file_contents(self):
        presets.save_presets([{"name": "First"}], self.path)
        presets.save_presets([{"name": "Second"}], self.path)
        loaded = presets.load_presets(self.path, self.builtin_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "Second")

    def test_malformed_json_reseeds_from_builtins_instead_of_crashing(self):
        self.path.write_text("{not valid json,,,")
        self.assertEqual(presets.load_presets(self.path, self.builtin_path), self.builtin_presets)

    def test_malformed_json_is_backed_up_and_the_file_is_reseeded(self):
        # Different from a plain "return empty and move on" recovery --
        # the corrupt file is preserved for manual recovery (renamed, not
        # deleted) *and* presets.json exists again afterward with the
        # built-ins, rather than being left missing.
        self.path.write_text("{not valid json,,,")
        presets.load_presets(self.path, self.builtin_path)
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        self.assertTrue(backup.exists())
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text())["presets"], self.builtin_presets)

    def test_wrong_top_level_shape_reseeds_from_builtins(self):
        # Valid JSON, but not the {"presets": [...]} shape this file expects.
        self.path.write_text('["just", "a", "list"]')
        self.assertEqual(presets.load_presets(self.path, self.builtin_path), self.builtin_presets)

    def test_presets_key_holding_non_list_reseeds_from_builtins(self):
        self.path.write_text('{"presets": "not a list"}')
        self.assertEqual(presets.load_presets(self.path, self.builtin_path), self.builtin_presets)


class TestLoadBuiltinPresets(unittest.TestCase):
    def test_loads_the_real_shipped_file(self):
        # Confirms the real, shipped builtin_presets.json (not a synthetic
        # fixture) is actually valid and has the shape the rest of the app
        # expects -- the file every fresh presets.json gets seeded from.
        loaded = presets.load_builtin_presets()
        # x265, x264, Intel VAAPI, AMD VAAPI -- 4 encoder profiles, each
        # with its own High/Balanced/Low trio.
        self.assertEqual(len(loaded), 12)
        names = {p["name"] for p in loaded}
        self.assertIn("720p Intel Balanced (Hardware / VAAPI)", names)
        self.assertIn("720p CPU Balanced (Software / x264)", names)

    def test_missing_file_raises_instead_of_silently_returning_empty(self):
        # Deliberately no try/except in load_builtin_presets -- a missing
        # shipped seed file is a packaging problem, not recoverable user
        # state the way a corrupt presets.json is.
        with self.assertRaises(OSError):
            presets.load_builtin_presets(Path(tempfile.mkdtemp()) / "nonexistent.json")


class TestMigrateMissingBuiltins(unittest.TestCase):
    """load_presets() only ever seeds from builtins when presets.json is
    missing or corrupt -- an already-installed presets.json otherwise
    stays frozen at whatever built-ins existed when it was first created,
    permanently missing anything added to builtin_presets.json afterward
    (a new encoder's own trio, say) unless something else fills the gap.
    Deliberately not part of load_presets() itself -- see that function's
    own test_save_overwrites_file_contents, which depends on it returning
    exactly what was saved, unmigrated."""

    def test_nothing_missing_returns_the_same_list_unchanged(self):
        builtins = [{"name": "A"}, {"name": "B"}]
        current = [{"name": "A"}, {"name": "B"}, {"name": "My Preset"}]
        self.assertEqual(presets.migrate_missing_builtins(current, builtins), current)

    def test_a_missing_builtin_is_inserted(self):
        builtins = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        current = [{"name": "A"}, {"name": "C"}]  # B was added to builtins later
        result = presets.migrate_missing_builtins(current, builtins)
        self.assertEqual([p["name"] for p in result], ["A", "B", "C"])

    def test_missing_builtin_lands_at_its_canonical_position_not_appended(self):
        # B belongs between A and C in builtins' own order -- appending it
        # at the end instead would be a real, visible ordering bug (the
        # preset combo would show A, C, ..., B instead of A, B, C).
        builtins = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        current = [{"name": "A"}, {"name": "C"}]
        result = presets.migrate_missing_builtins(current, builtins)
        self.assertEqual(result[1]["name"], "B")

    def test_user_presets_stay_after_every_builtin_and_keep_their_order(self):
        builtins = [{"name": "A"}, {"name": "B"}]
        current = [{"name": "A"}, {"name": "My First"}, {"name": "My Second"}]
        result = presets.migrate_missing_builtins(current, builtins)
        self.assertEqual(
            [p["name"] for p in result], ["A", "B", "My First", "My Second"]
        )

    def test_an_already_present_builtin_is_kept_as_loaded_not_overwritten_by_the_seed(self):
        # Built-ins are read-only in this app (Save As/Delete both refuse
        # to touch them) so the two should never actually differ in
        # practice -- but this confirms the merge doesn't *assume* that,
        # in case something upstream ever changes.
        builtins = [{"name": "A", "quality_value": 999}]
        current = [{"name": "A", "quality_value": 23}]
        result = presets.migrate_missing_builtins(current, builtins)
        self.assertEqual(result[0]["quality_value"], 23)

    def test_real_migration_adds_the_x264_trio_to_an_old_nine_preset_file(self):
        # End to end with the real shipped files, not synthetic fixtures
        # -- an old presets.json (from before libx264 existed) should
        # come out with all 12 current built-ins, x264's trio included.
        old_nine = [p for p in presets.load_builtin_presets() if "x264" not in p["name"]]
        self.assertEqual(len(old_nine), 9)  # sanity: this really is the pre-x264 shape
        result = presets.migrate_missing_builtins(old_nine, presets.load_builtin_presets())
        self.assertEqual(len(result), 12)
        names = [p["name"] for p in result]
        self.assertIn("720p CPU Balanced (Software / x264)", names)
        # Still grouped with the rest of the CPU family, not tacked on
        # at the very end past Intel/AMD.
        self.assertLess(names.index("720p CPU Balanced (Software / x264)"), names.index("720p Intel High (Hardware / VAAPI)"))


if __name__ == "__main__":
    unittest.main()
