"""
Shared pytest configuration.

Global artifact isolation
-------------------------
Much of the pipeline writes to the RELATIVE path ``Path("artifacts") / run_id``
(resolved against the current working directory). A test that exercises an engine
run or a stage node therefore drops a ``artifacts/<run_id>/`` directory into the
repo root unless it takes care to redirect it. Historically a handful of tests
did, leaving stale ``artifacts/test_*`` debris behind.

This autouse fixture runs EVERY test under a fresh per-test temp working
directory, so any such relative write lands in ``tmp_path/artifacts/...`` (auto
-cleaned by pytest) instead of polluting the real ``artifacts/`` tree. It is the
single, future-proof guard: a newly added test cannot leak no matter how it
constructs its run_id.

Safety: imports run at collection time (CWD = repo root, unaffected); only the
test *body* runs under the temp CWD. Tests load fixtures via package imports
(CWD-independent) and address their own outputs via ``tmp_path`` or the same
relative ``artifacts/`` path — both consistent under the chdir. Tests that
already call ``monkeypatch.chdir(tmp_path)`` themselves are unaffected (same dir).
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_artifacts_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
