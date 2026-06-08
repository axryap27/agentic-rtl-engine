"""
Tests for the artifacts/ run-directory naming + housekeeping helpers
(pipeline/run_dirs.py). All deterministic, no LLM calls — they operate on a
tmp base dir so the real artifacts/ tree is never touched.
"""

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from pipeline.run_dirs import (
    new_run_id,
    finalize_run_dir,
    clean_artifacts,
    update_latest_symlink,
)


def _make_run(base: Path, run_id: str, *, module: str | None = None,
              prompt: str = "x") -> Path:
    """Create a run dir under base with the 00_nl_spec marker (+ optional summary)."""
    d = base / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "00_nl_spec.json").write_text(json.dumps({"prompt": prompt}))
    if module is not None:
        (d / "01_summary.json").write_text(json.dumps({"module_name": module}))
    return d


# --------------------------------------------------------------------------- #
# new_run_id
# --------------------------------------------------------------------------- #

def test_new_run_id_is_date_prefixed_and_sortable():
    rid = new_run_id(now=datetime(2026, 6, 8, 15, 30, 45), token="abc123")
    assert rid == "2026-06-08/153045-abc123"


def test_new_run_id_unique_tokens():
    a, b = new_run_id(), new_run_id()
    # Random 6-hex suffix → effectively always distinct.
    assert a != b
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d{6}-[0-9a-f]{6}", a)


# --------------------------------------------------------------------------- #
# finalize_run_dir — module splice + latest symlink
# --------------------------------------------------------------------------- #

def test_finalize_splices_module_name(tmp_path):
    d = _make_run(tmp_path, "2026-06-08/153045-abc123", module="traffic_light_fsm")
    final, run_id = finalize_run_dir(d, base=tmp_path)
    assert final.name == "153045-traffic_light_fsm-abc123"
    assert run_id == "2026-06-08/153045-traffic_light_fsm-abc123"
    assert final.exists() and not d.exists()
    # Artifacts moved with the rename.
    assert (final / "00_nl_spec.json").exists()


def test_finalize_updates_latest_symlink(tmp_path):
    d = _make_run(tmp_path, "2026-06-08/153045-abc123", module="alu")
    final, _ = finalize_run_dir(d, base=tmp_path)
    link = tmp_path / "latest"
    assert link.is_symlink()
    assert link.resolve() == final.resolve()
    # Target is stored relative to base (portable).
    assert os.readlink(link) == "2026-06-08/153045-alu-abc123"


def test_finalize_no_summary_leaves_dir_but_still_links(tmp_path):
    d = _make_run(tmp_path, "2026-06-08/153045-abc123")  # no 01_summary.json
    final, run_id = finalize_run_dir(d, base=tmp_path)
    assert final == d  # unchanged leaf
    assert run_id == "2026-06-08/153045-abc123"
    assert (tmp_path / "latest").resolve() == d.resolve()


def test_finalize_sanitizes_module_name(tmp_path):
    d = _make_run(tmp_path, "2026-06-08/153045-abc123", module="my mod/v2!")
    final, _ = finalize_run_dir(d, base=tmp_path)
    assert final.name == "153045-my_mod_v2-abc123"


def test_finalize_is_idempotent_when_module_already_present(tmp_path):
    d = _make_run(tmp_path, "2026-06-08/153045-alu-abc123", module="alu")
    final, _ = finalize_run_dir(d, base=tmp_path)
    assert final == d  # module already in the leaf → no rename


# --------------------------------------------------------------------------- #
# update_latest_symlink
# --------------------------------------------------------------------------- #

def test_update_latest_symlink_replaces_previous(tmp_path):
    a = _make_run(tmp_path, "2026-06-08/100000-aaaaaa")
    b = _make_run(tmp_path, "2026-06-08/110000-bbbbbb")
    update_latest_symlink(a, base=tmp_path)
    update_latest_symlink(b, base=tmp_path)
    assert (tmp_path / "latest").resolve() == b.resolve()


# --------------------------------------------------------------------------- #
# clean_artifacts — prune by recency, only real runs
# --------------------------------------------------------------------------- #

def test_clean_keeps_newest_n_runs(tmp_path):
    for i in range(5):
        d = _make_run(tmp_path, f"2026-06-08/1000{i}0-r{i}")
        # Stagger mtimes so "newest" is well-defined.
        os.utime(d, (1000 + i, 1000 + i))
    deleted, kept = clean_artifacts(keep=2, base=tmp_path)
    assert (deleted, kept) == (3, 2)
    survivors = {p.parent.name for p in tmp_path.rglob("00_nl_spec.json")}
    assert survivors == {"100040-r4", "100030-r3"}


def test_clean_ignores_non_run_dirs(tmp_path):
    # A test/probe scratch dir with NO 00_nl_spec.json must never be pruned.
    probe = tmp_path / "test_backtrack_scratch"
    probe.mkdir()
    (probe / "refinement_chain.json").write_text("[]")
    for i in range(3):
        _make_run(tmp_path, f"2026-06-08/2000{i}0-r{i}")
    deleted, kept = clean_artifacts(keep=1, base=tmp_path)
    assert deleted == 2
    assert probe.exists()  # untouched


def test_clean_removes_empty_date_dirs(tmp_path):
    old = _make_run(tmp_path, "2026-06-07/090000-old")
    keep_new = _make_run(tmp_path, "2026-06-08/120000-new")
    os.utime(old, (1000, 1000))       # older
    os.utime(keep_new, (2000, 2000))  # newer → survives keep=1
    clean_artifacts(keep=1, base=tmp_path)
    # The 2026-06-07 date dir is now empty → swept.
    assert not (tmp_path / "2026-06-07").exists()
    assert (tmp_path / "2026-06-08").exists()


def test_clean_clears_dangling_latest(tmp_path):
    d = _make_run(tmp_path, "2026-06-08/120000-gone")
    update_latest_symlink(d, base=tmp_path)
    clean_artifacts(keep=0, base=tmp_path)  # delete everything
    assert not (tmp_path / "latest").exists()


def test_clean_empty_base_is_noop(tmp_path):
    assert clean_artifacts(keep=5, base=tmp_path) == (0, 0)
