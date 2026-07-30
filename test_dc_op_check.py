"""Regression tests for the Layer-3a DC operating-point checker.

The synthetic fixture pins down basic runner correctness (voltage divider,
inductor-as-short, ferrite-as-short). The v2alt scenario locks in the
discovery of bug #8 (LM5176 FB divider sized 110k/10k → FB=1.0V, but the
LM5176 internal ref is 1.2V → regulator would actually settle at ~14.4V,
not the labelled 12V)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "sim_harness/dc_op_check.py"
COMPONENTS_DIR = REPO_ROOT / "components"
FIXTURE_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/divider.net.xml"
FIXTURE_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/divider_scenario.json"


def _run_checker(scenario: Path, netlist: Path, work_dir: Path):
    env = os.environ.copy()
    env["TMPDIR"] = str(work_dir)
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(scenario),
         "--netlist", str(netlist),
         "--components-dir", str(COMPONENTS_DIR),
         "--work-dir", str(work_dir)],
        capture_output=True, text=True, env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _ngspice_writable():
    """Sandbox-permissive-enough environment check. ngspice needs to write
    /tmp/tmp* via glibc tmpfile(). If we can't, skip these tests rather
    than fail — they'll be exercised when a human runs them."""
    try:
        Path("/tmp/tmp_sim_harness_probe").touch()
        Path("/tmp/tmp_sim_harness_probe").unlink()
        return True
    except OSError:
        return False


needs_ngspice_tmp = pytest.mark.skipif(
    not _ngspice_writable(),
    reason="/tmp/tmp* not writable; see feedback_ngspice_sandbox memory",
)


@needs_ngspice_tmp
def test_synthetic_divider_passes(tmp_path):
    """R1(10k)+R2(2k) divider from 12V → MID=2.0V; L/FB behave as DC shorts."""
    rc, out, _ = _run_checker(FIXTURE_SCENARIO, FIXTURE_NETLIST, tmp_path)
    assert rc == 0, f"synthetic fixture unexpectedly failed:\n{out}"
    assert "3 pass, 0 fail, 0 missing" in out
    assert "MID = +2.0000 V" in out
    assert "AFTER_L = +12.0000 V" in out
    assert "AFTER_FB = +12.0000 V" in out


@needs_ngspice_tmp
def test_v2alt_full_pack_scenario_flags_fb_divider_bug(tmp_path):
    """Locks in bug #8 discovery: LM5176 FB divider (110k/10k) produces
    FB=1.0V given VOUT=12V, but the datasheet reference is 1.2V. The check
    must FAIL, showing the actual 1.0V vs expected 1.2V. If this test starts
    passing, either the divider was fixed (great — update the assertion) or
    the LM5176 spec's dc_model was changed in a way that hides the bug."""
    scenario = REPO_ROOT / "projects/power_module_v2alt/scenarios/full_pack_20v_usb.json"
    netlist = tmp_path / "v2alt.net.xml"
    subprocess.run(
        ["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
         "-o", str(netlist),
         str(REPO_ROOT / "projects/power_module_v2alt/power_module_v2alt.kicad_sch")],
        check=True,
    )
    rc, out, _ = _run_checker(scenario, netlist, tmp_path)
    assert rc == 1
    assert "FAIL" in out and "/buckboost/FB" in out
    assert "+1.0000 V" in out  # what the current 110k/10k divider actually gives
    assert "expected +1.2000" in out
