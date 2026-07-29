"""Regression tests: the checker must flag the three known LM5176 bugs
on `projects/power_module_v2alt` and no false positives on the same
schematic.  Bug context: [[project-lm5176-bugs-to-fix]]."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMATIC = REPO_ROOT / "projects/power_module_v2alt/power_module_v2alt.kicad_sch"
CHECKER = REPO_ROOT / "sim_harness/static_check.py"
COMPONENTS_DIR = REPO_ROOT / "components"


@pytest.fixture(scope="module")
def netlist_xml(tmp_path_factory):
    xml_path = tmp_path_factory.mktemp("net") / "v2alt.net.xml"
    subprocess.run(
        ["kicad-cli", "sch", "export", "netlist", "--format", "kicadxml",
         "-o", str(xml_path), str(SCHEMATIC)],
        check=True,
    )
    return xml_path


@pytest.fixture(scope="module")
def checker_output(netlist_xml):
    proc = subprocess.run(
        [sys.executable, str(CHECKER),
         "--netlist", str(netlist_xml),
         "--components-dir", str(COMPONENTS_DIR),
         str(SCHEMATIC)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def test_exits_nonzero_when_bugs_present(checker_output):
    rc, _ = checker_output
    assert rc == 1


def test_flags_mode_pin_direct_to_gnd(checker_output):
    _, out = checker_output
    assert "U1_BUCKBOOST1 pin 4" in out
    assert "MODE" in out and "AGND" in out
    assert "must_connect_through" in out


def test_flags_slope_pin_has_resistor(checker_output):
    _, out = checker_output
    assert "U1_BUCKBOOST1 pin 7" in out
    assert "SLOPE" in out
    assert "R5_BUCKBOOST1" in out and "resistor" in out


def test_flags_cs_pin_on_switching_node(checker_output):
    _, out = checker_output
    assert "U1_BUCKBOOST1 pin 16" in out
    assert "CS" in out and "SWITCHING_NODE" in out
    assert "SW1" in out


def test_no_unexpected_errors_beyond_the_three(checker_output):
    _, out = checker_output
    error_lines = [ln for ln in out.splitlines() if ln.startswith("[ERROR")]
    # exactly three known errors, all on U1_BUCKBOOST1 pins 4, 7, 16
    assert len(error_lines) == 3, error_lines
    for ln in error_lines:
        assert "U1_BUCKBOOST1" in ln
