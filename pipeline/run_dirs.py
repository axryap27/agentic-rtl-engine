"""
Run-directory naming + housekeeping for the artifacts/ tree.

The pipeline locates every artifact via ``Path("artifacts") / run_id``. By making
``run_id`` a date-prefixed, human-readable relative path, the on-disk layout
becomes self-describing and time-sortable instead of a flat pile of opaque
hashes:

    artifacts/
      2026-06-08/
        115234-traffic_light_fsm-15d3dd/
        121530-alu-6f8272/
      latest -> 2026-06-08/121530-alu-6f8272/

The module name is not known until Stage 1 runs, so the leaf is created as
``<HHMMSS>-<shorthash>`` up front and the module is spliced in *after* the run
completes (``finalize_run_dir``). A trailing short hash keeps every run unique
even for two runs of the same module in the same second.

These helpers are pure with respect to a ``base`` directory so they can be unit
tested in a tmp dir without invoking the LLM pipeline. NO LLM calls here.
"""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

ARTIFACTS_BASE = Path("artifacts")
_LATEST = "latest"
# Every real run writes this first (main.py seeds the chain with it). Its presence
# is how we tell a genuine run dir from a test/probe scratch dir.
_RUN_MARKER = "00_nl_spec.json"


def _sanitize(name: str) -> str:
    """Reduce a module name to a filesystem- and glob-safe token."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_")[:40]


def new_run_id(now: datetime | None = None, token: str | None = None) -> str:
    """Return a fresh date-prefixed run_id: ``YYYY-MM-DD/HHMMSS-<6hex>``.

    The returned string is used verbatim as ``run_id``; the slash makes the date
    a real subdirectory under ``artifacts/`` once joined via ``Path / run_id``.

    Args:
        now: timestamp to stamp (defaults to ``datetime.now()`` — injected in tests).
        token: 6-char uniqueness suffix (defaults to a random uuid fragment).
    """
    now = now or datetime.now()
    token = token or uuid.uuid4().hex[:6]
    return f"{now:%Y-%m-%d}/{now:%H%M%S}-{token}"


def update_latest_symlink(target_dir: Path, base: Path = ARTIFACTS_BASE) -> None:
    """Point ``base/latest`` at ``target_dir`` (best-effort; never raises).

    The link target is stored relative to ``base`` so the tree stays portable.
    Filesystems without symlink support (or a permission error) are tolerated —
    the symlink is a convenience, not a correctness requirement.
    """
    link = base / _LATEST
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target_dir.relative_to(base))
    except OSError:
        pass


def finalize_run_dir(
    artifact_dir: Path,
    base: Path = ARTIFACTS_BASE,
) -> tuple[Path, str]:
    """Splice the module name into the run dir leaf and refresh ``latest``.

    Reads ``<artifact_dir>/01_summary.json`` for ``module_name`` and renames the
    leaf ``<HHMMSS>-<hash>`` → ``<HHMMSS>-<module>-<hash>``. If the summary is
    missing, unreadable, has no module name, or a same-named target already
    exists, the directory is left as-is. The ``latest`` symlink is always updated
    to the final directory.

    Returns ``(final_dir, run_id)`` where ``run_id`` is the final dir relative to
    ``base`` (POSIX-style), suitable for re-locating the run later.
    """
    final_dir = artifact_dir
    module = None
    summary = artifact_dir / "01_summary.json"
    if summary.exists():
        try:
            import json

            module = json.loads(summary.read_text()).get("module_name")
        except Exception:
            module = None

    if module:
        module = _sanitize(str(module))
    if module and module not in artifact_dir.name:
        leaf = artifact_dir.name
        parts = leaf.split("-")
        # leaf is "<HHMMSS>-<hash>"; insert module between the two.
        stamp, tail = parts[0], parts[-1]
        new_leaf = f"{stamp}-{module}-{tail}"
        candidate = artifact_dir.parent / new_leaf
        if candidate != artifact_dir and not candidate.exists():
            try:
                artifact_dir.rename(candidate)
                final_dir = candidate
            except OSError:
                final_dir = artifact_dir

    update_latest_symlink(final_dir, base)
    try:
        run_id = final_dir.relative_to(base).as_posix()
    except ValueError:
        run_id = final_dir.name
    return final_dir, run_id


def _run_dirs(base: Path) -> list[Path]:
    """All genuine run directories under ``base`` (those holding the run marker)."""
    return sorted(
        {p.parent for p in base.rglob(_RUN_MARKER)},
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )


def clean_artifacts(keep: int = 10, base: Path = ARTIFACTS_BASE) -> tuple[int, int]:
    """Delete all but the ``keep`` most-recent run directories.

    A "run" is any directory containing ``00_nl_spec.json`` (so test/probe scratch
    dirs without that marker are never touched). After pruning, now-empty date
    subdirectories are removed and a dangling ``latest`` symlink is cleared.

    Returns ``(deleted_count, kept_count)``.
    """
    if not base.exists():
        return 0, 0
    runs = _run_dirs(base)
    keep = max(0, keep)
    to_delete = runs[keep:]
    for d in to_delete:
        shutil.rmtree(d, ignore_errors=True)

    # Sweep empty date subdirectories left behind by the deletions.
    for child in base.iterdir():
        if child.is_dir() and not child.is_symlink():
            try:
                next(child.rglob("*"))
            except StopIteration:
                shutil.rmtree(child, ignore_errors=True)

    # Drop a now-dangling latest symlink.
    link = base / _LATEST
    if link.is_symlink() and not link.resolve().exists():
        try:
            link.unlink()
        except OSError:
            pass

    return len(to_delete), min(keep, len(runs))
