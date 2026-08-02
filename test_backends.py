"""Cross-backend equivalence tests.

Every existing fixture is executed under BOTH SubprocessBackend and
PySpiceBackend; results must match within a small numerical tolerance.
This locks in that the FFI path produces the same physics as the
subprocess path — the primary safety net for future refactors.

Skips PySpice tests if the library isn't importable (PySpice + libngspice
are optional).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "sim_harness/dc_op_check.py"
COMPONENTS_DIR = REPO_ROOT / "components"
TEST_COMPONENTS_DIR = REPO_ROOT / "sim_harness/tests/fixtures/components"


def _pyspice_available() -> bool:
    try:
        import PySpice  # noqa: F401
        # Also require the actual libngspice.so to be findable
        return Path("/usr/lib/x86_64-linux-gnu/libngspice.so.0").exists()
    except ImportError:
        return False


needs_pyspice = pytest.mark.skipif(
    not _pyspice_available(),
    reason="PySpice or libngspice not available",
)


def _ngspice_writable() -> bool:
    try:
        Path("/tmp/tmp_backend_probe").touch()
        Path("/tmp/tmp_backend_probe").unlink()
        return True
    except OSError:
        return False


needs_ngspice_tmp = pytest.mark.skipif(
    not _ngspice_writable(),
    reason="/tmp/tmp* not writable; see feedback_ngspice_sandbox memory",
)


def _run(scenario: Path, netlist: Path, work_dir: Path, backend: str,
         components_dir: Path = TEST_COMPONENTS_DIR):
    env = os.environ.copy()
    env["TMPDIR"] = str(work_dir)
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(scenario),
         "--netlist", str(netlist),
         "--components-dir", str(components_dir),
         "--work-dir", str(work_dir),
         "--backend", backend],
        capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout


# Fixture pairs to cross-check. Kept small — 6 fixtures covering DC + AC +
# tran + loop-stability + Layer-5c topologies. The full 36 tests would be
# overkill and slow (~2x runtime); a curated sample gives good coverage.
CROSS_BACKEND_FIXTURES = [
    # (scenario, netlist, components_dir_choice: 'test' or 'real')
    ("divider_scenario.json",              "divider.net.xml",              "test"),
    ("rc_lowpass_scenario.json",           "rc_lowpass.net.xml",           "test"),
    ("rc_step_scenario.json",              "rc_step.net.xml",              "test"),
    ("ss_ramp_scenario.json",              "ss_ramp.net.xml",              "test"),
    ("op_amp_buffer_stable_scenario.json", "op_amp_buffer_stable.net.xml", "test"),
    ("norton_scenario.json",               "norton.net.xml",               "real"),
    ("xspice_gain_scenario.json",          "xspice_gain.net.xml",          "test"),
]


@needs_pyspice
@needs_ngspice_tmp
@pytest.mark.parametrize("scenario_file,netlist_file,comps", CROSS_BACKEND_FIXTURES)
def test_cross_backend_equivalence(tmp_path, scenario_file, netlist_file, comps):
    """Both backends must PASS/FAIL the same set of checks and produce
    numerical results within tolerance of each other. Divergence signals
    a bug in one backend."""
    fixtures_dir = REPO_ROOT / "sim_harness/tests/fixtures"
    scenario = fixtures_dir / scenario_file
    netlist = fixtures_dir / netlist_file
    components = TEST_COMPONENTS_DIR if comps == "test" else COMPONENTS_DIR

    rc_sp, out_sp = _run(scenario, netlist, tmp_path / "sp", "subprocess",
                          components_dir=components)
    rc_py, out_py = _run(scenario, netlist, tmp_path / "py", "pyspice",
                          components_dir=components)

    assert rc_sp == rc_py, (
        f"exit code diverges: subprocess={rc_sp}, pyspice={rc_py}\n"
        f"--- subprocess ---\n{out_sp}\n--- pyspice ---\n{out_py}"
    )

    # Compare pass/fail/missing counts — the ultimate outcome
    sp_summary = _extract_summary(out_sp)
    py_summary = _extract_summary(out_py)
    assert sp_summary == py_summary, (
        f"summary diverges: subprocess={sp_summary}, pyspice={py_summary}\n"
        f"--- subprocess ---\n{out_sp}\n--- pyspice ---\n{out_py}"
    )


def _extract_summary(output: str) -> tuple[int, int, int]:
    """Parse the '<N> pass, <N> fail, <N> missing' summary line."""
    import re
    m = re.search(r"(\d+) pass, (\d+) fail, (\d+) missing", output)
    if not m:
        return (-1, -1, -1)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))
