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
NORTON_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/norton.net.xml"
NORTON_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/norton_scenario.json"
DIODE_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/diode.net.xml"
DIODE_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/diode_scenario.json"
MOSFET_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/mosfet_switch.net.xml"
MOSFET_ON_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/mosfet_switch_on_scenario.json"
MOSFET_OFF_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/mosfet_switch_off_scenario.json"
FAIL_DC_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/fail_dc_checks.net.xml"
FAIL_DC_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/fail_dc_checks_scenario.json"
TEST_COMPONENTS_DIR = REPO_ROOT / "sim_harness/tests/fixtures/components"
RC_LP_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/rc_lowpass.net.xml"
RC_LP_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/rc_lowpass_scenario.json"
RC_STEP_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/rc_step.net.xml"
RC_STEP_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/rc_step_scenario.json"
OPAMP_UNITY_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_unity.net.xml"
OPAMP_UNITY_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_unity_scenario.json"
OPAMP_INV_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_inverting.net.xml"
OPAMP_INV_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_inverting_scenario.json"
HALFBUFF_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/half_buff.net.xml"
HALFBUFF_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/half_buff_scenario.json"
OPAMP_SUPPRESS_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_unity_suppress.net.xml"
HALFBUFF_PRECEDENCE_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/half_buff_precedence.net.xml"
DIVIDER_MISSING_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/divider_missing_scenario.json"
RC_LP_FAIL_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/rc_lowpass_fail_scenario.json"
RC_STEP_FAIL_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/rc_step_fail_scenario.json"
OPAMP_BUF_STABLE_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_buffer_stable.net.xml"
OPAMP_BUF_STABLE_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_buffer_stable_scenario.json"
OPAMP_BUF_UNSTABLE_NETLIST = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_buffer_unstable.net.xml"
OPAMP_BUF_UNSTABLE_SCENARIO = REPO_ROOT / "sim_harness/tests/fixtures/op_amp_buffer_unstable_scenario.json"


def _run_checker(scenario: Path, netlist: Path, work_dir: Path,
                 components_dir: Path = COMPONENTS_DIR,
                 keep_deck: bool = False):
    env = os.environ.copy()
    env["TMPDIR"] = str(work_dir)
    cmd = [sys.executable, str(CHECKER), str(scenario),
           "--netlist", str(netlist),
           "--components-dir", str(components_dir),
           "--work-dir", str(work_dir)]
    if keep_deck:
        cmd.append("--keep-deck")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
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
    must FAIL, showing the actual 1.0V vs the LM5176's 1.2V reference. As
    of Layer 3b the FB assertion lives in the LM5176 spec, not the scenario
    — the check fires automatically for any project that uses this IC."""
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
    # Bug #8 (FB divider): must fail, showing 1.0V vs spec's 1.2V ref
    assert "FAIL" in out and "U1_BUCKBOOST1 pin 11 (FB)" in out
    assert "+1.0000 V" in out
    assert "must equal 1.2 V" in out
    # Bug #3 (MODE tied to GND): flagged by dc_check must_be_in_range
    assert "U1_BUCKBOOST1 pin 4 (MODE)" in out and "must be in [1.38, 7.6]" in out
    # Bugs #6, #7 (CH224K VDD/VBUS-sense direct on VBUS_RAW): flagged by must_be_below
    assert "U1_INPUT1 pin 1 (VDD)" in out and "+20.0000 V" in out
    assert "U1_INPUT1 pin 8 (VBUS)" in out and "must be below 13.5 V" in out
    # LM5176 EN/UVLO divider is correctly biased above threshold
    assert "PASS" in out and "EN/UVLO" in out and "must exceed 1.22 V" in out


@needs_ngspice_tmp
def test_layer_3c_diode_drop(tmp_path):
    """Diode in series with a resistor to GND: V(cathode-side) ≈ VIN - V_F.
    Verifies ngspice's Newton solver converges with our generic diode model
    and that our runner correctly identifies pin 2 as anode / pin 1 as cathode
    per KiCad convention."""
    rc, out, _ = _run_checker(DIODE_SCENARIO, DIODE_NETLIST, tmp_path)
    assert rc == 0, f"diode fixture unexpectedly failed:\n{out}"
    assert "PASS" in out and "MID" in out
    # V_F comes out ≈ 0.65V at ~0.44mA for D_GENERIC
    assert "+4.3" in out  # 4.35 ± tolerance, allow either 4.3xx or 4.4xx


@needs_ngspice_tmp
def test_layer_3c_mosfet_switch_on(tmp_path):
    """N-channel MOSFET as low-side switch, gate driven above V_TH. Drain
    must be pulled close to GND (well below VIN)."""
    rc, out, _ = _run_checker(MOSFET_ON_SCENARIO, MOSFET_NETLIST, tmp_path)
    assert rc == 0, f"mosfet-on fixture unexpectedly failed:\n{out}"
    assert "PASS" in out and "DRAIN" in out
    # Drain should be in the low mV range, definitely < 100mV
    import re
    m = re.search(r"DRAIN = ([+-]?\d+\.\d+) V", out)
    assert m, out
    v_drain = float(m.group(1))
    assert v_drain < 0.1, f"expected drain well below VIN when FET is on; got {v_drain}"


@needs_ngspice_tmp
def test_layer_3c_mosfet_switch_off(tmp_path):
    """Same fixture, gate at 0V → FET off → drain floats to VIN."""
    rc, out, _ = _run_checker(MOSFET_OFF_SCENARIO, MOSFET_NETLIST, tmp_path)
    assert rc == 0, f"mosfet-off fixture unexpectedly failed:\n{out}"
    assert "PASS" in out and "DRAIN" in out
    assert "+5.0000 V" in out


@needs_ngspice_tmp
def test_layer_4a_rc_lowpass(tmp_path):
    """RC low-pass fixture (R=1k, C=1µF, f_c ≈ 159 Hz). Verifies AC sweep
    infrastructure: -3 dB at cutoff, -20 dB/decade rolloff. Checks 4
    frequencies spanning 10 Hz to 15.9 kHz."""
    rc, out, _ = _run_checker(RC_LP_SCENARIO, RC_LP_NETLIST, tmp_path)
    assert rc == 0, f"AC fixture unexpectedly failed:\n{out}"
    assert "4 pass, 0 fail, 0 missing" in out
    # Cutoff should land within 0.5 dB of theoretical -3 dB
    import re
    m = re.search(r"@ 159 Hz.*VOUT: ([+-]?\d+\.\d+) dB", out)
    assert m, f"cutoff point not found in output:\n{out}"
    v_at_cutoff = float(m.group(1))
    assert -3.5 <= v_at_cutoff <= -2.5, f"cutoff off: {v_at_cutoff} dB"


@needs_ngspice_tmp
def test_layer_5a_rc_step_response(tmp_path):
    """RC step-response fixture (R=1k, C=1µF, τ=1ms). VIN steps 0→5V at
    t=1µs; V(VOUT) charges as 5(1-e^(-t/τ)). Verifies transient sweep
    infrastructure: step_source PWL emission, .tran directive, time-domain
    output parsing, expected-time-point comparison."""
    rc, out, _ = _run_checker(RC_STEP_SCENARIO, RC_STEP_NETLIST, tmp_path)
    assert rc == 0, f"tran fixture unexpectedly failed:\n{out}"
    assert "4 pass, 0 fail, 0 missing" in out
    # V(τ) ≈ 3.16V — verify it lands inside tolerance
    import re
    m = re.search(r"@ 0\.001 s.*VOUT: ([+-]?\d+\.\d+) V", out)
    assert m, f"τ time-point not found:\n{out}"
    v_tau = float(m.group(1))
    assert 3.10 <= v_tau <= 3.22, f"V(τ) off: {v_tau}V (theory 3.161V)"


@needs_ngspice_tmp
def test_negative_all_dc_check_kinds_fire(tmp_path):
    """FAIL_TESTBED_L3B has 4 dc_checks (one per kind: must_equal /
    must_exceed / must_be_below / must_be_in_range) that are all
    constructed to fail at 10V. Verifies each check kind emits FAIL
    with the correct description, and that rc=1 fires."""
    rc, out, _ = _run_checker(FAIL_DC_SCENARIO, FAIL_DC_NETLIST, tmp_path,
                              components_dir=TEST_COMPONENTS_DIR)
    assert rc == 1
    fail_lines = [ln for ln in out.splitlines() if ln.startswith("[FAIL")]
    assert len(fail_lines) == 4, f"expected 4 FAILs, got {len(fail_lines)}:\n{out}"
    # each kind's description appears
    assert any("must equal 5 V"        in ln for ln in fail_lines), out
    assert any("must exceed 15 V"      in ln for ln in fail_lines), out
    assert any("must be below 5 V"     in ln for ln in fail_lines), out
    assert any("must be in [0, 5]"     in ln for ln in fail_lines), out
    # rationale is echoed on failing lines
    assert "negative-path: must_equal" in out


@needs_ngspice_tmp
def test_ac_model_unity_gain_buffer(tmp_path):
    """Op-amp ac_model → auto-generated single-pole .subckt. Unity-gain
    feedback: DC = 1.0V (ideal), AC flat at 0 dB below GBW=1 MHz, -3 dB
    at GBW. Verifies subckt emission, X-instance wiring, and that the
    A0/GBW numbers produce the theoretical response."""
    rc, out, _ = _run_checker(OPAMP_UNITY_SCENARIO, OPAMP_UNITY_NETLIST, tmp_path,
                              components_dir=TEST_COMPONENTS_DIR)
    assert rc == 0, f"unity-gain buffer fixture unexpectedly failed:\n{out}"
    assert "4 pass, 0 fail, 0 missing" in out
    assert "VOUT = +1.0000 V" in out
    # -3 dB at GBW should land between -2.5 and -3.5
    import re
    m = re.search(r"@ 1e\+06 Hz.*VOUT: ([+-]?\d+\.\d+) dB", out)
    assert m, f"GBW point not found:\n{out}"
    v = float(m.group(1))
    assert -3.5 <= v <= -2.5, f"GBW gain off: {v} dB"


@needs_ngspice_tmp
def test_ac_model_inverting_amp(tmp_path):
    """Op-amp ac_model combined with external R_in=10k, R_f=100k. DC gain
    = -R_f/R_in = -10 (20 dB). Closed-loop -3 dB = GBW · β ≈ 90.9 kHz.
    Verifies ac_model composes with external components and produces the
    predicted closed-loop bandwidth reshape."""
    rc, out, _ = _run_checker(OPAMP_INV_SCENARIO, OPAMP_INV_NETLIST, tmp_path,
                              components_dir=TEST_COMPONENTS_DIR)
    assert rc == 0, f"inverting-amp fixture unexpectedly failed:\n{out}"
    assert "4 pass, 0 fail, 0 missing" in out
    assert "VOUT = -0.999" in out  # -0.9999V ideal, small A0 error
    # At the closed-loop -3 dB point, expect ~17 dB
    import re
    m = re.search(r"@ 90910 Hz.*VOUT: ([+-]?\d+\.\d+) dB", out)
    assert m, f"closed-loop BW point not found:\n{out}"
    v = float(m.group(1))
    assert 16.5 <= v <= 17.5, f"closed-loop -3 dB off: {v} dB"


@needs_ngspice_tmp
def test_behavioral_spice_subckt_include_path(tmp_path):
    """Externally-supplied .subckt: TESTHALFBUFF is 3-terminal, defined in
    half_buff.sub (E1 out gnd in gnd 0.5). Verifies .include emission (path
    resolved relative to the spec file), X-instance terminal ordering
    (pin_index → subckt position), and that per-pin dc_models on those
    pins are suppressed."""
    rc, out, _ = _run_checker(HALFBUFF_SCENARIO, HALFBUFF_NETLIST, tmp_path,
                              components_dir=TEST_COMPONENTS_DIR)
    assert rc == 0, f"half-buff subckt fixture unexpectedly failed:\n{out}"
    assert "1 pass, 0 fail, 0 missing" in out
    assert "VOUT = +0.5000 V" in out


@needs_ngspice_tmp
def test_negative_ac_model_suppresses_per_pin_dc_model(tmp_path):
    """TESTOP_SUPPRESS declares BOTH ac_model AND a per-pin dc_model
    (driven 5.0V) on OUT. The ac_model must win — VOUT should read
    ~1.0V (op-amp unity-gain behavior), NOT 5.0V (driven behavior).
    If suppression regresses, ngspice will either double-drive VOUT
    to 5V or fail to solve."""
    rc, out, _ = _run_checker(OPAMP_UNITY_SCENARIO, OPAMP_SUPPRESS_NETLIST, tmp_path,
                              components_dir=TEST_COMPONENTS_DIR)
    assert rc == 0, f"suppression test unexpectedly failed:\n{out}"
    assert "VOUT = +1.0000 V" in out
    assert "+5.0000" not in out, "OUT pin dc_model=5V leaked through — ac_model didn't suppress it"


@needs_ngspice_tmp
def test_negative_behavioral_subckt_precedence_over_ac_model(tmp_path):
    """TESTHALFBUFF_AC declares BOTH behavioral_spice_subckt (0.5×
    half-buffer) AND ac_model (op-amp). behavioral must win — the
    generated deck should .include half_buff.sub and NOT emit any
    OPAMP_SP_ subckt. Runtime: VOUT = 0.5V (half-buffer)."""
    rc, out, _ = _run_checker(HALFBUFF_SCENARIO, HALFBUFF_PRECEDENCE_NETLIST, tmp_path,
                              components_dir=TEST_COMPONENTS_DIR,
                              keep_deck=True)
    assert rc == 0, f"precedence test unexpectedly failed:\n{out}"
    assert "VOUT = +0.5000 V" in out
    # Inspect the emitted deck: only behavioral X-instance, no ac_model subckt
    deck = (tmp_path / "half_buff_dc.cir").read_text()
    assert ".include" in deck and "half_buff.sub" in deck, deck
    assert "OPAMP_SP_" not in deck, "ac_model subckt leaked into deck when behavioral was set"
    assert "TESTHALFBUFF" in deck, deck


@needs_ngspice_tmp
def test_negative_missing_node_reports_missing_and_exits_nonzero(tmp_path):
    """Scenario `expected` references GHOST_NODE which isn't in the
    divider netlist. Must produce a MISSING status and rc=1 — silent
    pass on unknown nodes would let typos in scenarios hide."""
    rc, out, _ = _run_checker(DIVIDER_MISSING_SCENARIO, FIXTURE_NETLIST, tmp_path)
    assert rc == 1, f"missing-node case should fail; got rc=0:\n{out}"
    assert "MISSING" in out and "GHOST_NODE" in out
    assert "0 pass, 0 fail, 1 missing" in out


@needs_ngspice_tmp
def test_negative_ac_wrong_magnitude_fails(tmp_path):
    """AC expected magnitude at cutoff asserted as 0 dB (actual: -3 dB).
    Locks in that the Layer-4a evaluator returns FAIL when actual
    diverges from expected — a silent-pass bug would let wrong
    magnitudes ship."""
    rc, out, _ = _run_checker(RC_LP_FAIL_SCENARIO, RC_LP_NETLIST, tmp_path)
    assert rc == 1, f"AC fail case should exit nonzero; got rc=0:\n{out}"
    assert "FAIL" in out and "vin_to_vout @ 159 Hz" in out
    assert "0 pass, 1 fail" in out


@needs_ngspice_tmp
def test_negative_tran_wrong_voltage_fails(tmp_path):
    """Transient expected voltage at t=τ asserted as 10V (actual: 3.16V).
    Locks in that the Layer-5a evaluator returns FAIL."""
    rc, out, _ = _run_checker(RC_STEP_FAIL_SCENARIO, RC_STEP_NETLIST, tmp_path)
    assert rc == 1, f"tran fail case should exit nonzero; got rc=0:\n{out}"
    assert "FAIL" in out and "vin_step @ 0.001 s" in out
    assert "0 pass, 1 fail" in out


@needs_ngspice_tmp
def test_layer_4b_stable_buffer_healthy_margin(tmp_path):
    """Unity-gain buffer with light (100 pF) cap load. Loop broken at
    U1/IN- (sense pin). Middlebrook voltage injection sweeps 1 Hz to
    100 MHz. Expect crossover ≈ GBW (1 MHz) and PM close to 90° (single-
    pole-dominant behavior). Verifies the entire Layer-4b pipeline —
    loop-break rewrite, V_loop_inj emission, T(f) extraction, margin
    interpolation — end-to-end."""
    rc, out, _ = _run_checker(OPAMP_BUF_STABLE_SCENARIO, OPAMP_BUF_STABLE_NETLIST,
                              tmp_path, components_dir=TEST_COMPONENTS_DIR)
    assert rc == 0, f"stable-buffer loop test unexpectedly failed:\n{out}"
    assert "2 pass, 0 fail, 0 missing" in out
    # Crossover should land near GBW = 1 MHz
    import re
    m = re.search(r"crossover_hz: \+([\d.e+]+) Hz", out)
    assert m, f"crossover not extracted:\n{out}"
    fc = float(m.group(1))
    assert 5e5 <= fc <= 2e6, f"crossover out of expected range: {fc} Hz"
    # PM should be close to 90° for a dominant-pole system
    m = re.search(r"phase_margin_deg: \+([\d.]+)", out)
    assert m, f"PM not extracted:\n{out}"
    pm = float(m.group(1))
    assert 80 <= pm <= 95, f"PM off for single-pole: {pm}°"


@needs_ngspice_tmp
def test_layer_4b_cap_loaded_buffer_flags_instability(tmp_path):
    """Same op-amp + unity-gain feedback, but with a heavy (100 nF) cap
    load. The parasitic pole from Rout·C_load lands ~2 decades below
    GBW, pushing phase toward -180° at crossover → PM well below 45°.
    Expected: PM check FAILS (fixture designed to be unstable). This is
    the negative-path test that proves Layer 4b actually catches the
    cap-load stability failure mode."""
    rc, out, _ = _run_checker(OPAMP_BUF_UNSTABLE_SCENARIO, OPAMP_BUF_UNSTABLE_NETLIST,
                              tmp_path, components_dir=TEST_COMPONENTS_DIR)
    assert rc == 1, f"cap-loaded buffer should have FAIL'd PM check; rc=0:\n{out}"
    assert "FAIL" in out and "phase_margin_deg" in out
    import re
    m = re.search(r"phase_margin_deg: \+([\d.]+)", out)
    assert m, f"PM not extracted:\n{out}"
    pm = float(m.group(1))
    assert pm < 30, f"expected severely degraded PM (<30°); got {pm}°"


@needs_ngspice_tmp
def test_norton_fixture_passes(tmp_path):
    """Norton dc_model: SYNTH_ISRC sources 100µA out of pin 1 into R1(10k)→GND;
    Ohm's law gives V=1.0V. The spec's dc_check verifies this without a
    scenario-side expected block — proves both the Norton emission AND the
    dc_check evaluation path."""
    rc, out, _ = _run_checker(NORTON_SCENARIO, NORTON_NETLIST, tmp_path)
    assert rc == 0, f"norton fixture unexpectedly failed:\n{out}"
    assert "1 pass, 0 fail, 0 missing" in out
    assert "U1 pin 1 (IOUT)" in out
    assert "+1.0000 V" in out
