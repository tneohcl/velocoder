"""core/ must stay Qt-free (the project-layout standard): the engine logic
has to be importable and testable without PySide6 or a display."""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE = REPO_ROOT / "src" / "velocoder" / "core"


class CoreIsQtFree(unittest.TestCase):
    def test_no_core_module_imports_qt(self):
        for path in sorted(CORE.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    self.assertFalse(name.startswith(("PySide6", "shiboken6")), f"{path.name} imports {name}")

    def test_importing_core_loads_no_qt(self):
        modules = sorted(f"velocoder.core.{p.stem}" for p in CORE.glob("*.py") if p.stem != "__init__")
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
            + "".join(f"import {m}\n" for m in modules)
            + "print(sorted(m for m in sys.modules if m.startswith(('PySide6', 'shiboken6'))))\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
