"""The suite must never read or write the real user's settings, saved queue or
home folder (tests/_sandbox.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sandbox  # noqa: E402,F401  (first: see its docstring)

import os  # noqa: E402
import re  # noqa: E402
import unittest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from PySide6.QtCore import QSettings  # noqa: E402

from velocoder.core.constants import APP_NAME  # noqa: E402
from velocoder.ui import session  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent


class SandboxTests(unittest.TestCase):
    def test_settings_saved_queue_and_home_resolve_inside_the_sandbox(self):
        root = Path(_sandbox.ROOT)
        # Same constructor as MainWindow and HelpWindow use.
        for path in (QSettings(APP_NAME, APP_NAME).fileName(), session.session_file_path(), Path.home()):
            with self.subTest(path=str(path)):
                self.assertTrue(Path(path).is_relative_to(root), path)

    def test_every_test_module_imports_the_sandbox_before_qt_or_the_app(self):
        first_app_import = re.compile(r"^\s*(from|import) (PySide6|velocoder)\b", re.M)
        for module in sorted(TESTS_DIR.glob("test_*.py")):
            with self.subTest(module.name):
                text = module.read_text()
                sandbox = text.find("import _sandbox")
                self.assertNotEqual(sandbox, -1, "missing `import _sandbox`")
                first = first_app_import.search(text)
                if first:
                    self.assertLess(sandbox, first.start())

    def test_the_real_home_is_not_where_the_suite_runs(self):
        self.assertNotEqual(Path.home(), Path(_sandbox.REAL_HOME))
        self.assertTrue(os.environ["XDG_CONFIG_HOME"].startswith(_sandbox.ROOT))


if __name__ == "__main__":
    unittest.main()
