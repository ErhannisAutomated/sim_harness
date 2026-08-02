"""Simulation-backend abstraction.

Two implementations:
  - `SubprocessBackend`  — spawns `ngspice -b <deck.cir>` per run and parses
    stdout. The original path, still the default for portability (works
    anywhere ngspice is installed).
  - `PySpiceBackend`     — loads the netlist into libngspice via PySpice's
    NgSpiceShared, dispatches control commands via exec_command, reads
    result vectors directly from ngspice's plot store. Faster (no fork+exec
    per test) and produces structured data (no fragile stdout regex).

Both backends implement `run(netlist, commands, expected_nodes) -> RunResult`
so upstream code doesn't care which one is executing.

Cross-backend equivalence is enforced by `test_backends.py` — every existing
fixture must produce matching results under both backends.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from spice import (
    deck_to_text, parse_node_voltages, parse_ac_output, parse_tran_output,
    spice_node,
)


@dataclass
class RunResult:
    """Uniform result shape for both backends.

    Populated based on the analysis type of the commands passed in. For a
    `.op` (DC operating point), only `node_voltages` is filled. For `.ac`,
    `ac_samples` is filled. For `.tran`, `tran_samples` is filled. Mixed-
    analysis runs (e.g. op + ac in one deck) can fill multiple.
    """
    node_voltages: dict[str, float] = field(default_factory=dict)
    ac_samples: dict[str, list[tuple[float, complex]]] = field(default_factory=dict)
    tran_samples: dict[str, list[tuple[float, float]]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subprocess backend
# ---------------------------------------------------------------------------

class SubprocessBackend:
    """Shells out to `ngspice -b <deck.cir>`. Portable — works with any
    ngspice binary on PATH."""

    name = "subprocess"

    def __init__(self, work_dir: Path, tag: str = "run", keep_deck: bool = False):
        self.work_dir = work_dir
        self.tag = tag
        self.keep_deck = keep_deck

    def run(self, netlist: str, commands: list[str],
            expected_output_nodes: Optional[list[str]] = None,
            cm_paths: Optional[list[str]] = None) -> RunResult:
        deck_text = deck_to_text(netlist, commands)
        deck_path = self.work_dir / f"{self.tag}.cir"
        deck_path.write_text(deck_text)
        env = os.environ.copy()
        env["TMPDIR"] = str(self.work_dir)
        # XSPICE codemodels must load BEFORE the netlist is parsed. In
        # subprocess mode the only supported channel is a .spiceinit file
        # read at ngspice startup — routed by setting HOME to a fresh dir
        # so we don't touch the user's real ~/.spiceinit.
        if cm_paths:
            spice_home = self.work_dir / f"{self.tag}_home"
            spice_home.mkdir(exist_ok=True)
            init_lines = [f"codemodel {p}" for p in cm_paths]
            (spice_home / ".spiceinit").write_text("\n".join(init_lines) + "\n")
            env["HOME"] = str(spice_home)
        proc = subprocess.run(
            ["ngspice", "-b", str(deck_path)],
            capture_output=True, text=True, timeout=60, env=env,
        )
        output = proc.stdout + "\n---STDERR---\n" + proc.stderr
        result = RunResult()
        analysis = _analysis_from_commands(commands)
        if analysis == "dc":
            result.node_voltages = parse_node_voltages(output)
        elif analysis == "ac":
            result.ac_samples = parse_ac_output(output, expected_nodes=expected_output_nodes)
        elif analysis == "tran":
            result.tran_samples = parse_tran_output(output, expected_nodes=expected_output_nodes)
        if not self.keep_deck and deck_path.exists():
            # Caller may still want the deck for debug; leave it on failure
            pass
        return result


# ---------------------------------------------------------------------------
# PySpice (libngspice FFI) backend
# ---------------------------------------------------------------------------

# Path to libngspice.so — PySpice defaults to unversioned `libngspice.so`,
# which Debian's `libngspice0` doesn't ship (only versioned .so.0 is
# installed). Callers can override via env var if the default is wrong.
_DEFAULT_LIBNGSPICE_PATH = "/usr/lib/x86_64-linux-gnu/libngspice.so.0"


def _resolve_libngspice_path() -> str:
    return os.environ.get("SIM_HARNESS_LIBNGSPICE", _DEFAULT_LIBNGSPICE_PATH)


class PySpiceBackend:
    """In-process libngspice via PySpice's NgSpiceShared.

    Each `run()` creates a fresh NgSpiceShared instance and disposes of it
    to avoid cross-run state leakage. That's slower than sharing one
    instance across many runs, but keeps semantics identical to
    SubprocessBackend (which forks a clean ngspice per run). Sharing an
    instance across many runs is a future optimization if warranted."""

    name = "pyspice"

    def __init__(self, work_dir: Path, tag: str = "run"):
        self.work_dir = work_dir  # unused; kept for interface symmetry
        self.tag = tag

    def run(self, netlist: str, commands: list[str],
            expected_output_nodes: Optional[list[str]] = None,
            cm_paths: Optional[list[str]] = None) -> RunResult:
        ns = _new_ngspice_instance()
        # XSPICE .cm files must be loaded BEFORE load_circuit() —
        # otherwise A-line references to their models fail at parse time.
        for cm in (cm_paths or []):
            ns.exec_command(f"codemodel {cm}")
        # Feed the netlist (netlist ends with a blank line; add `.end` so
        # ngspice accepts it as a self-contained circuit).
        ns.load_circuit(netlist + ".end\n")
        # Dispatch commands. PySpice's exec_command raises on ngspice-side
        # errors; propagate rather than swallow.
        for cmd in commands:
            if cmd.startswith("print") or cmd == "let out = 0":
                # We read vectors directly from the plot store — print
                # commands are stdout-noise-only in PySpice mode.
                continue
            ns.exec_command(cmd)

        result = RunResult()
        analysis = _analysis_from_commands(commands)
        plot_name = ns.last_plot
        if plot_name == "const":
            # No analysis ran (e.g. all commands were skipped). Empty result.
            return result
        plot = ns.plot(simulation=None, plot_name=plot_name)

        if analysis == "dc":
            for name, vec in plot.items():
                if name in ("time", "frequency"):
                    continue
                arr = _vector_to_ndarray(vec, plot)
                if arr is not None and len(arr) > 0:
                    result.node_voltages[name.lower()] = float(arr[0].real)
        elif analysis == "ac":
            freq_arr = _vector_to_ndarray(plot["frequency"], plot)
            for node in (expected_output_nodes or []):
                key = _find_plot_key(plot, node)
                if key is None:
                    continue
                arr = _vector_to_ndarray(plot[key], plot)
                if arr is None:
                    continue
                # freq_arr is complex; take real part
                result.ac_samples[node.lower()] = [
                    (float(f.real), complex(v)) for f, v in zip(freq_arr, arr)
                ]
        elif analysis == "tran":
            time_arr = _vector_to_ndarray(plot["time"], plot)
            for node in (expected_output_nodes or []):
                key = _find_plot_key(plot, node)
                if key is None:
                    continue
                arr = _vector_to_ndarray(plot[key], plot)
                if arr is None:
                    continue
                result.tran_samples[node.lower()] = [
                    (float(t.real), float(v.real)) for t, v in zip(time_arr, arr)
                ]
        return result


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _analysis_from_commands(commands: list[str]) -> str:
    """Return 'dc' | 'ac' | 'tran' based on which analysis the commands
    invoke. Defaults to 'dc' if nothing recognized."""
    for c in commands:
        head = c.strip().split(None, 1)[0].lower() if c.strip() else ""
        if head == "ac":  return "ac"
        if head == "tran": return "tran"
        if head == "op":  return "dc"
    return "dc"


def _new_ngspice_instance():
    """Import PySpice lazily and return a fresh NgSpiceShared instance.
    We import inside the function so callers that only use SubprocessBackend
    don't need PySpice installed at all."""
    # Set library path BEFORE importing NgSpiceShared reads it at import time.
    # Also filter PySpice's "Unsupported Ngspice version 44" warning —
    # cosmetic only, functionality verified working.
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared
    NgSpiceShared.LIBRARY_PATH = _resolve_libngspice_path()
    # Silence the version warning by intercepting stderr just for the
    # constructor call.
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        ns = NgSpiceShared.new_instance()
    _warn_if_important(buf.getvalue())
    return ns


_VERSION_WARN_RE = re.compile(r"Unsupported Ngspice version \d+")


def _warn_if_important(stderr_text: str):
    """Pass through anything from PySpice's stderr that isn't the cosmetic
    'Unsupported Ngspice version' banner."""
    for line in stderr_text.splitlines():
        if not line.strip():
            continue
        if _VERSION_WARN_RE.search(line):
            continue
        print(f"pyspice: {line}", file=sys.stderr)


def _vector_to_ndarray(vec, plot):
    """PySpice's Vector.to_waveform() needs the abscissa passed in for
    ordinate vectors. Returns None if extraction fails."""
    try:
        # Abscissa vectors (time, frequency) don't need themselves passed
        if vec is plot.get("time") or vec is plot.get("frequency"):
            wf = vec.to_waveform(to_real=True)
        else:
            abscissa = plot.get("time") or plot.get("frequency")
            wf = vec.to_waveform(abscissa=abscissa, to_real=False)
        import numpy as np
        return np.array(wf)
    except Exception as e:
        warnings.warn(f"failed to extract vector: {e}")
        return None


def _find_plot_key(plot, node: str) -> Optional[str]:
    """PySpice strips the v() wrapping — vectors are stored under bare
    node names, potentially with case differences from what our SPICE
    emission uses. Match case-insensitively."""
    node_lc = node.lower()
    for k in plot.keys():
        if k.lower() == node_lc:
            return k
    # Fallback: prefix match (ngspice truncates long names)
    for k in plot.keys():
        if node_lc.startswith(k.lower()) or k.lower().startswith(node_lc):
            return k
    return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_backend(name: str, work_dir: Path, tag: str = "run",
                 keep_deck: bool = False):
    """Return a backend instance by name. 'subprocess' or 'pyspice'."""
    if name == "subprocess":
        return SubprocessBackend(work_dir, tag=tag, keep_deck=keep_deck)
    if name == "pyspice":
        return PySpiceBackend(work_dir, tag=tag)
    raise ValueError(f"unknown backend {name!r} (choose subprocess | pyspice)")
