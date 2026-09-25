"""The bundled odcs-ui must be exactly the pinned release (packaging/sync-odcs-ui.sh)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import themes  # noqa: E402,F401  (puts vendor/ on sys.path)
import odcs_ui  # noqa: E402


class VendoredOdcsUi(unittest.TestCase):
    def test_bundled_version_matches_the_pin(self):
        tag, commit = (ROOT / "vendor" / "ODCS_UI_VERSION").read_text().split()
        self.assertEqual("v" + odcs_ui.__version__, tag)
        self.assertEqual(len(commit), 40)

    def test_velocoder_imports_the_bundled_copy(self):
        self.assertEqual(Path(odcs_ui.__file__).resolve().parent, ROOT / "vendor" / "odcs_ui")

    def test_bundled_copy_is_complete(self):
        names = {p.name for p in (ROOT / "vendor" / "odcs_ui").iterdir()}
        for required in ("tokens.json", "base.qss", "theming.py", "widgets.py", "timefmt.py", "LICENSE"):
            self.assertIn(required, names)

    def test_colour_tokens_are_the_shared_ones(self):
        from odcs_ui import tokens
        for name in ("dark", "light"):
            shared = tokens.THEMES[name]
            self.assertEqual({k: v for k, v in themes.THEMES[name].items() if k in shared}, shared)


if __name__ == "__main__":
    unittest.main()
