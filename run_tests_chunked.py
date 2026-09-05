#!/usr/bin/env python3
"""Run the test suite in small batches, each in its own fresh process.

A single long-lived process running the whole suite accumulates Qt/PySide6
resources across the many MainWindow() instances test_main.py constructs --
confirmed the hard way: an unchunked `python3 -m unittest discover` run was
still not done after 4.5 hours (189 of 387 tests, RSS climbing throughout).
Chunking sidesteps this by tearing the process down every CHUNK_SIZE tests
instead of relying on Qt to release everything itself. Local dev and CI
both use this instead of `unittest discover` directly.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TESTS_DIR = REPO_ROOT / "tests"
CHUNK_SIZE = 20


def discover_test_ids():
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR))
    ids = []

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            for sub in item:
                walk(sub)
        else:
            ids.append(item.id())

    walk(suite)
    return ids


def batches(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def main():
    test_ids = discover_test_ids()
    if not test_ids:
        print("No tests discovered.", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(TESTS_DIR), env.get("PYTHONPATH", "")])
    )

    all_batches = list(batches(test_ids, CHUNK_SIZE))
    print(
        f"Running {len(test_ids)} tests in {len(all_batches)} batches "
        f"of up to {CHUNK_SIZE}\n",
        flush=True,
    )

    failed = []
    for i, batch in enumerate(all_batches, start=1):
        print(f"=== Batch {i}/{len(all_batches)} ({len(batch)} tests) ===", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", *batch],
            cwd=str(REPO_ROOT),
            env=env,
        )
        if result.returncode != 0:
            failed.append(i)
            print(f"*** Batch {i} FAILED ***", flush=True)

    print(f"\n=== {len(all_batches)} batches run, {len(failed)} failed ===")
    if failed:
        print(f"Failed batches: {failed}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
