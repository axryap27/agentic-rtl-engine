"""Shared subprocess wrappers for external verification / synthesis tools.

Each `run_*` function knows how to invoke its tool and parse its output
into a structured dataclass.  Every tool call:
  - Captures stdout + stderr
  - Enforces a timeout
  - Returns a structured result the caller can act on directly

Currently implemented:
  - run_tla2sany      TLA+ syntax/semantic checker
  - run_tlc           TLC model checker

More tools (pcal.trans, iverilog, yosys, verilator) will be added as the
later pipeline stages need them.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# tla2tools.jar discovery
# ---------------------------------------------------------------------------

def _find_tla_jar() -> Optional[Path]:
    """Locate tla2tools.jar.  Env override > VS Code extension bundle."""
    env = os.environ.get("TLA_TOOLS_JAR")
    if env and Path(env).exists():
        return Path(env)
    candidates = sorted(
        Path("/Users/aarya/.vscode/extensions").glob(
            "tlaplus.vscode-ide-*/tools/tla2tools.jar"
        ),
        reverse=True,
    )
    return candidates[0] if candidates else None


TLA_TOOLS_JAR: Optional[Path] = _find_tla_jar()


# ---------------------------------------------------------------------------
# Generic subprocess runner
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def combined(self) -> str:
        if self.stderr:
            return self.stdout + "\n" + self.stderr
        return self.stdout


def _run(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: float = 120.0,
) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return CommandResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            returncode=-1,
            stdout=(exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")),
            stderr=(exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
                   + f"\n[timeout after {timeout}s]",
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(
            returncode=-2,
            stdout="",
            stderr=f"Command not found: {exc}",
        )


# ---------------------------------------------------------------------------
# tla2sany — TLA+ syntax / semantic checker
# ---------------------------------------------------------------------------

@dataclass
class SanyResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    raw_output: str = ""

    @classmethod
    def unavailable(cls, reason: str) -> "SanyResult":
        return cls(ok=False, errors=[f"tla2sany unavailable: {reason}"])


_SANY_ERROR_PATTERNS = (
    re.compile(r"^\*\*\*\s*Errors"),                # "*** Errors:" section header
    re.compile(r"Parse Error"),                     # "Parse Error" line
    re.compile(r"^Error\b"),                        # "Error: ..."
    re.compile(r"Abort.*encountered"),              # "Abort messages encountered"
    re.compile(r"^Unknown operator"),
    re.compile(r"^Could not find module"),
)


def run_tla2sany(tla_path: Path, timeout: float = 60.0) -> SanyResult:
    """Run TLA+ syntax/semantic checker on a .tla file.

    Returns SanyResult(ok=True) iff sany exits 0 AND no error patterns
    appear in its output.  Errors are captured as a list of message lines
    suitable for injecting into a retry prompt.
    """
    if TLA_TOOLS_JAR is None:
        return SanyResult.unavailable("tla2tools.jar not found")
    if not tla_path.exists():
        return SanyResult.unavailable(f".tla file does not exist: {tla_path}")

    # Run from the .tla file's directory so EXTENDS resolves local modules.
    cmd = ["java", "-cp", str(TLA_TOOLS_JAR), "tla2sany.SANY", tla_path.name]
    result = _run(cmd, cwd=tla_path.parent, timeout=timeout)
    raw = result.combined

    errors: list[str] = []
    # Walk lines and capture anything matching an error pattern.  Once we
    # hit an error pattern we also capture the following indented detail
    # lines until a blank line.
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(p.search(line) for p in _SANY_ERROR_PATTERNS):
            errors.append(line.rstrip())
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].startswith("***"):
                errors.append(lines[j].rstrip())
                j += 1
            i = j
            continue
        i += 1

    ok = result.returncode == 0 and not errors
    return SanyResult(ok=ok, errors=errors, raw_output=raw)


# ---------------------------------------------------------------------------
# TLC — TLA+ model checker
# ---------------------------------------------------------------------------

@dataclass
class TLCResult:
    ok: bool
    invariant_violated: Optional[str] = None
    counterexample: list[str] = field(default_factory=list)
    states_explored: Optional[int] = None
    error_summary: str = ""
    raw_output: str = ""
    timed_out: bool = False

    @classmethod
    def unavailable(cls, reason: str) -> "TLCResult":
        return cls(ok=False, error_summary=f"TLC unavailable: {reason}")


def run_tlc(
    tla_path: Path,
    cfg_path: Path,
    timeout: float = 120.0,
    skip_deadlock_check: bool = True,
) -> TLCResult:
    """Run TLC model checker on a .tla / .cfg pair.

    Returns TLCResult(ok=True) iff TLC reports
        "Model checking completed. No error has been found."
    Otherwise the result carries the invariant name violated and the
    counterexample trace (state-by-state), ready to inject into a retry
    prompt.

    `skip_deadlock_check=True` passes `-deadlock` to TLC which DISABLES
    deadlock checking.  Free-running hardware specs that loop forever
    would otherwise trigger spurious deadlock errors when TLC reaches a
    cycle.
    """
    if TLA_TOOLS_JAR is None:
        return TLCResult.unavailable("tla2tools.jar not found")
    if not tla_path.exists():
        return TLCResult.unavailable(f".tla file does not exist: {tla_path}")
    if not cfg_path.exists():
        return TLCResult.unavailable(f".cfg file does not exist: {cfg_path}")

    cmd = [
        "java", "-cp", str(TLA_TOOLS_JAR), "tlc2.TLC",
        "-config", cfg_path.name,
    ]
    if skip_deadlock_check:
        cmd.append("-deadlock")
    cmd.append(tla_path.name)

    result = _run(cmd, cwd=tla_path.parent, timeout=timeout)
    raw = result.combined

    invariant_violated: Optional[str] = None
    counterexample: list[str] = []
    states_explored: Optional[int] = None

    lines = raw.splitlines()
    in_trace = False
    for i, line in enumerate(lines):
        m = re.search(r"Invariant (\S+) is violated", line)
        if m:
            invariant_violated = m.group(1)

        m = re.search(r"(\d+) states generated", line)
        if m:
            states_explored = int(m.group(1))

        if "The behavior up to this point is:" in line:
            in_trace = True
            continue

        if in_trace:
            # Trace ends when we hit statistics or a new section.
            if (
                "states generated" in line
                or "states left on queue" in line
                or line.startswith("Finished in")
                or line.startswith("Trace exploration spec path")
            ):
                in_trace = False
                continue
            if line.strip():
                counterexample.append(line.rstrip())

    if result.timed_out:
        return TLCResult(
            ok=False,
            timed_out=True,
            raw_output=raw,
            error_summary=f"TLC timed out after {timeout}s",
        )

    success_marker = "Model checking completed. No error has been found." in raw
    ok = (
        result.returncode == 0
        and success_marker
        and invariant_violated is None
    )

    summary = ""
    if not ok:
        if invariant_violated:
            summary = f"Invariant {invariant_violated} violated"
        else:
            for line in lines:
                if line.startswith("Error:"):
                    summary = line.strip()
                    break
            if not summary:
                summary = f"TLC did not complete successfully (returncode={result.returncode})"

    return TLCResult(
        ok=ok,
        invariant_violated=invariant_violated,
        counterexample=counterexample,
        states_explored=states_explored,
        error_summary=summary,
        raw_output=raw,
    )
