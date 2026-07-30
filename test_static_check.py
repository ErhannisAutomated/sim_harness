"""Regression tests: the checker must flag the known real bugs on
`projects/power_module_v2alt` and nothing else at ERROR severity.

Bug context: [[project-lm5176-bugs-to-fix]] + first-real-use findings
from the 4-IC multi-agent extraction (2026-07-29)."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMATIC = REPO_ROOT / "projects/power_module_v2alt/power_module_v2alt.kicad_sch"
CHECKER = REPO_ROOT / "sim_harness/static_check.py"
COMPONENTS_DIR = REPO_ROOT / "components"


# Each entry: (substring-match assertions on a single [ERROR ...] output line).
# Order-independent; each entry must match exactly one distinct error line.
EXPECTED_ERRORS = [
    # LM5176 (buckboost controller) — original three bugs.
    ("U1_BUCKBOOST1 pin 4", "MODE", "AGND", "forbidden_direct_connection"),
    ("U1_BUCKBOOST1 pin 7", "SLOPE", "capacitor", "R5_BUCKBOOST1"),
    ("U1_BUCKBOOST1 pin 16", "CS", "SW1", "forbidden_direct_connection"),
    # BQ76920 (BMS) — BAT pin missing R_f filter.
    ("U1_BMS1 pin 10", "BAT", "must_connect_through", "BAT+"),
    # IP2326 (charger) — ISET resistor programs charge current above IC max.
    ("U1_CHARGER1 pin 11", "ISET", "R8_CHARGER1", "33k"),
    # CH224K (USB-C PD sink) — VDD directly on VBUS_RAW (abs-max 3.6V, VBUS→20V).
    # (forbidden_direct_connection is deduped away since must_connect_through covers the same case)
    ("U1_INPUT1 pin 1", "VDD", "VBUS_RAW", "must_connect_through"),
    # CH224K — VBUS-sense pin also directly on VBUS_RAW (abs-max 13.5V, VBUS→20V).
    ("U1_INPUT1 pin 8", "VBUS", "VBUS_RAW", "must_connect_through"),
]


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


def _error_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("[ERROR")]


def test_exits_nonzero_when_bugs_present(checker_output):
    rc, _ = checker_output
    assert rc == 1


@pytest.mark.parametrize("substrings", EXPECTED_ERRORS)
def test_expected_error_present(checker_output, substrings):
    """Each expected finding must appear in exactly one error line."""
    _, out = checker_output
    matches = [ln for ln in _error_lines(out)
               if all(s in ln for s in substrings)]
    assert len(matches) == 1, (
        f"Expected exactly one error matching all of {substrings!r}; "
        f"found {len(matches)}:\n" + "\n".join(matches)
    )


def test_no_unexpected_errors(checker_output):
    """No error line may appear that doesn't match one of the expected patterns.
    If this test fires, either a new real bug was found (add to
    EXPECTED_ERRORS) or a false-positive regressed (fix the checker/spec)."""
    _, out = checker_output
    errors = _error_lines(out)
    unmatched = []
    for ln in errors:
        if not any(all(s in ln for s in tup) for tup in EXPECTED_ERRORS):
            unmatched.append(ln)
    assert not unmatched, "Unexpected errors:\n" + "\n".join(unmatched)


def test_error_count_matches_expected(checker_output):
    _, out = checker_output
    assert len(_error_lines(out)) == len(EXPECTED_ERRORS)
