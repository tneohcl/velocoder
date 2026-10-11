"""Keeps the test suite out of the real user's files.

Every test module imports this first, before Qt or the app. It points HOME
and the XDG config/data/cache/state dirs at a throwaway directory, so
QSettings (~/.config/VeloCoder/VeloCoder.conf: theme, window geometry,
Expert state), QStandardPaths (session.json, the saved queue) and the
default output folder (~/Videos/transcoded) all resolve inside it. Before
this, closing a test's MainWindow saved its geometry and theme into the
real config, and the next real launch opened with them.

Set before any QSettings exists: Qt reads these paths once per process.
One directory per process (the chunked runner starts one per batch),
removed at exit."""
import atexit
import os
import shutil
import tempfile

REAL_HOME = os.path.expanduser("~")
ROOT = tempfile.mkdtemp(prefix="velocoder-tests-")

os.environ["HOME"] = ROOT
for _var, _sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                   ("XDG_CACHE_HOME", ".cache"), ("XDG_STATE_HOME", ".local/state")):
    os.environ[_var] = os.path.join(ROOT, _sub)
    os.makedirs(os.environ[_var], exist_ok=True)

atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
