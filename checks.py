"""Evaluation of scenario expectations and component-spec assertions
against a solved SPICE operating point / AC sweep.

Extracted from dc_op_check.py. Nothing here emits SPICE or runs ngspice —
that's spice.py's job. This module takes parsed voltages / AC samples
plus the scenario + specs, and produces Result / CheckResult / AcResult
records ready for printing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from spice import pin_net_lookup, spice_node
from static_check import Netlist, match_spec


# ---------------------------------------------------------------------------
# Scenario `expected` block (Layer 3a DC values)
# ---------------------------------------------------------------------------

@dataclass
class Result:
    node: str
    expected_v: float
    tolerance_v: float
    actual_v: Optional[float]
    status: str        # 'pass' | 'fail' | 'missing'


def compare_expected(voltages: dict[str, float], scenario: dict) -> list[Result]:
    results: list[Result] = []
    for net_name, expected in scenario.get("expected", {}).items():
        sn = spice_node(net_name).lower()
        v = voltages.get(sn)
        tol = expected.get("tolerance_v", 0.05)
        exp = expected["v"]
        if v is None:
            status = "missing"
        elif abs(v - exp) <= tol:
            status = "pass"
        else:
            status = "fail"
        results.append(Result(node=net_name, expected_v=exp,
                              tolerance_v=tol, actual_v=v, status=status))
    return results


def format_results(results: list[Result]) -> str:
    lines = []
    for r in results:
        tag = {"pass": "PASS   ", "fail": "FAIL   ", "missing": "MISSING"}[r.status]
        if r.actual_v is not None:
            lines.append(f"[{tag}] {r.node} = {r.actual_v:+.4f} V "
                         f"(expected {r.expected_v:+.4f} ±{r.tolerance_v})")
        else:
            lines.append(f"[{tag}] {r.node} not found in DC output "
                         f"(expected {r.expected_v:+.4f} ±{r.tolerance_v})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Component-spec `dc_checks` (Layer 3b)
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    ic_ref: str
    pin: str
    pin_name: str
    check_kind: str
    description: str
    actual_v: Optional[float]
    status: str         # 'pass' | 'fail' | 'missing'
    rationale: str = ""


def evaluate_dc_checks(nl: Netlist, specs: dict,
                       voltages: dict[str, float]) -> list[CheckResult]:
    """Evaluate every pin-level dc_check across the design.
    Returns one CheckResult per (component × pin × check)."""
    results: list[CheckResult] = []
    for comp in nl.comps.values():
        spec_hit = match_spec(comp, specs)
        if spec_hit is None:
            continue
        spec = spec_hit[1]
        for pin in spec.get("pins", []):
            checks = pin.get("dc_checks") or []
            if not checks:
                continue
            candidates = pin["index"] if isinstance(pin["index"], list) else [pin["index"]]
            resolved = pin_net_lookup(comp.ref, [str(c) for c in candidates], nl)
            if resolved is None:
                for c in checks:
                    results.append(CheckResult(
                        ic_ref=comp.ref, pin=str(candidates[0]),
                        pin_name=pin["name"], check_kind=c["kind"],
                        description=_describe_check(c),
                        actual_v=None, status="missing",
                        rationale=c.get("rationale", ""),
                    ))
                continue
            matched_idx, net = resolved
            sn = spice_node(net).lower()
            # SPICE node "0" is ground by definition; ngspice's print output
            # omits it, so look it up as an implicit 0.0V.
            actual = 0.0 if sn == "0" else voltages.get(sn)
            for c in checks:
                if actual is None:
                    status = "missing"
                else:
                    status = "pass" if _check_passes(c, actual) else "fail"
                results.append(CheckResult(
                    ic_ref=comp.ref, pin=matched_idx, pin_name=pin["name"],
                    check_kind=c["kind"], description=_describe_check(c),
                    actual_v=actual, status=status,
                    rationale=c.get("rationale", ""),
                ))
    return results


def _describe_check(c: dict) -> str:
    k = c["kind"]
    if k == "must_equal":
        tol = c.get("tolerance_v", 0.05)
        return f"must equal {c['voltage_v']:g} V ±{tol:g}"
    if k == "must_exceed":
        return f"must exceed {c['threshold_v']:g} V"
    if k == "must_be_below":
        return f"must be below {c['threshold_v']:g} V"
    if k == "must_be_in_range":
        return f"must be in [{c['min_v']:g}, {c['max_v']:g}] V"
    return k


def _check_passes(c: dict, v: float) -> bool:
    k = c["kind"]
    if k == "must_equal":
        return abs(v - c["voltage_v"]) <= c.get("tolerance_v", 0.05)
    if k == "must_exceed":
        return v > c["threshold_v"]
    if k == "must_be_below":
        return v < c["threshold_v"]
    if k == "must_be_in_range":
        return c["min_v"] <= v <= c["max_v"]
    return True  # unknown kind — don't fail-close on new rule kinds


# ---------------------------------------------------------------------------
# Scenario `ac_sweeps` (Layer 4a)
# ---------------------------------------------------------------------------

@dataclass
class AcResult:
    sweep_name: str
    freq_hz: float
    output_node: str
    expected_db: float
    tolerance_db: float
    actual_db: Optional[float]
    status: str
    rationale: str = ""


def _nearest_freq_sample(samples: list[tuple[float, complex]], target_hz: float
                          ) -> Optional[tuple[float, complex]]:
    if not samples:
        return None
    # log-space distance matches ngspice's decade/octave sweep spacing
    return min(samples, key=lambda s: abs(math.log10(s[0]) - math.log10(target_hz)))


def evaluate_ac_sweep(sweep_name: str, sweep: dict,
                       samples: dict[str, list[tuple[float, complex]]]
                       ) -> list[AcResult]:
    results: list[AcResult] = []
    default_out = sweep.get("output_node")
    for p in sweep.get("expected", []):
        out_node_raw = p.get("output_node", default_out)
        if out_node_raw is None:
            continue
        out_node = spice_node(out_node_raw).lower()
        freq = p["freq_hz"]
        expected_db = p["magnitude_db"]
        tol = p.get("tolerance_db", 1.0)
        rationale = p.get("rationale", "")
        block = samples.get(out_node)
        if not block:
            results.append(AcResult(sweep_name, freq, out_node_raw, expected_db,
                                    tol, None, "missing", rationale))
            continue
        nearest = _nearest_freq_sample(block, freq)
        if nearest is None:
            results.append(AcResult(sweep_name, freq, out_node_raw, expected_db,
                                    tol, None, "missing", rationale))
            continue
        _, cval = nearest
        mag = abs(cval)
        actual_db = 20 * math.log10(mag) if mag > 1e-300 else -300.0
        status = "pass" if abs(actual_db - expected_db) <= tol else "fail"
        results.append(AcResult(sweep_name, freq, out_node_raw, expected_db,
                                tol, actual_db, status, rationale))
    return results


# ---------------------------------------------------------------------------
# Scenario `loop_stability` (Layer 4b)
# ---------------------------------------------------------------------------

@dataclass
class LoopStabilityResult:
    loop_name: str
    metric: str          # 'crossover_hz' / 'phase_margin_deg' / 'gain_margin_db'
    expected_desc: str   # 'in [X, Y]' / 'min X' / etc — for printing
    actual: Optional[float]
    status: str          # 'pass' | 'fail' | 'missing'
    rationale: str = ""


def _log_interp(x_lo: float, x_hi: float, y_lo: float, y_hi: float,
                 x_target: float) -> float:
    """Linear interpolation in log-x space (leaves y linear)."""
    if x_lo == x_hi:
        return y_lo
    frac = (math.log(x_target) - math.log(x_lo)) / (math.log(x_hi) - math.log(x_lo))
    return y_lo + frac * (y_hi - y_lo)


def _find_zero_crossing_freq(ts: list[tuple[float, float]]
                              ) -> Optional[float]:
    """Given [(freq_hz, y), ...] sorted by freq, find the freq where y crosses
    zero (linearly interpolating in log-freq). Returns None if no crossing.
    Uses the FIRST zero crossing (loop gain typically drops monotonically)."""
    for i in range(len(ts) - 1):
        (f0, y0), (f1, y1) = ts[i], ts[i + 1]
        if y0 == 0:
            return f0
        if (y0 > 0) != (y1 > 0):
            # crosses between i and i+1
            frac = -y0 / (y1 - y0)
            return math.exp(math.log(f0) + frac * (math.log(f1) - math.log(f0)))
    return None


def _unwrapped_phase_deg(samples: list[tuple[float, complex]]
                          ) -> list[tuple[float, float]]:
    """Phase in degrees, monotonic (jumps > 180° unwrapped by adding ±360°).
    Returns [(freq, phase_deg), ...]."""
    out: list[tuple[float, float]] = []
    prev: Optional[float] = None
    for f, T in samples:
        p = math.degrees(math.atan2(T.imag, T.real))
        if prev is not None:
            while p - prev > 180:  p -= 360
            while p - prev < -180: p += 360
        out.append((f, p))
        prev = p
    return out


def evaluate_loop_stability(loop_name: str, loop_cfg: dict,
                             v_original: list[tuple[float, complex]],
                             v_injected: list[tuple[float, complex]]
                             ) -> list[LoopStabilityResult]:
    """Compute loop gain T(f) = -V(orig)/V(inj), extract crossover/PM/GM,
    compare to expected bounds.

    Both sample lists are assumed to share the same frequency grid (they
    come from the same ngspice AC run). Any frequency where |V(inj)| ≈ 0
    (numerical noise) is skipped."""
    # Zip by index (ngspice AC output is deterministic in freq order)
    if len(v_original) != len(v_injected):
        return [LoopStabilityResult(loop_name, "crossover_hz", "any", None,
                                     "missing", "AC output row counts differ")]
    # Middlebrook voltage loop-gain: T = -V(upstream)/V(downstream).
    # Convention: `break_at` names the FEEDBACK SENSE pin (op-amp IN-,
    # regulator FB, comparator IN). That way the driver stays connected
    # to its loads (loads' impedance shapes the response — critical for
    # cap-load stability). Signal flows driver → V_inj → sense; V_inj's
    # upstream is `original_node` (still carries the driver + loads);
    # downstream is `injected_node` (isolated on the sense pin).
    # → T = -V(original)/V(injected).
    T_samples: list[tuple[float, complex]] = []
    for (f_o, V_o), (f_i, V_i) in zip(v_original, v_injected):
        if abs(V_i) < 1e-30:
            continue
        T_samples.append((f_o, -V_o / V_i))
    if not T_samples:
        return [LoopStabilityResult(loop_name, "crossover_hz", "any", None,
                                     "missing", "loop gain samples empty")]

    mag_db = [(f, 20 * math.log10(abs(T))) for f, T in T_samples if abs(T) > 0]
    phase = _unwrapped_phase_deg(T_samples)

    # Crossover: first freq where mag crosses 0 dB
    crossover_hz = _find_zero_crossing_freq(mag_db)

    # Phase margin: 180° + phase(T at crossover)
    pm_deg: Optional[float] = None
    if crossover_hz is not None:
        # Interpolate phase at crossover
        for i in range(len(phase) - 1):
            f0, p0 = phase[i]
            f1, p1 = phase[i + 1]
            if f0 <= crossover_hz <= f1:
                p_at_cx = _log_interp(f0, f1, p0, p1, crossover_hz)
                pm_deg = 180.0 + p_at_cx
                break

    # Gain margin: -|T|_dB at freq where phase(T) = -180°
    # (search unwrapped phase for -180° crossing)
    gm_db: Optional[float] = None
    phase_offset = [(f, p + 180) for f, p in phase]
    f_180 = _find_zero_crossing_freq(phase_offset)
    if f_180 is not None:
        for i in range(len(mag_db) - 1):
            f0, m0 = mag_db[i]
            f1, m1 = mag_db[i + 1]
            if f0 <= f_180 <= f1:
                m_at_180 = _log_interp(f0, f1, m0, m1, f_180)
                gm_db = -m_at_180
                break

    expected = loop_cfg.get("expected", {})
    results: list[LoopStabilityResult] = []
    for metric_name, actual in (
        ("crossover_hz", crossover_hz),
        ("phase_margin_deg", pm_deg),
        ("gain_margin_db", gm_db),
    ):
        bound = expected.get(metric_name)
        if bound is None:
            continue
        desc = _describe_bound(bound)
        rationale = bound.get("rationale", "")
        if actual is None:
            results.append(LoopStabilityResult(
                loop_name, metric_name, desc, None, "missing", rationale))
            continue
        status = "pass" if _bound_passes(bound, actual) else "fail"
        results.append(LoopStabilityResult(
            loop_name, metric_name, desc, actual, status, rationale))
    return results


def _describe_bound(bound: dict) -> str:
    lo, hi = bound.get("min"), bound.get("max")
    if lo is not None and hi is not None:
        return f"in [{lo:g}, {hi:g}]"
    if lo is not None:
        return f"≥ {lo:g}"
    if hi is not None:
        return f"≤ {hi:g}"
    return "any"


def _bound_passes(bound: dict, actual: float) -> bool:
    lo, hi = bound.get("min"), bound.get("max")
    if lo is not None and actual < lo:
        return False
    if hi is not None and actual > hi:
        return False
    return True


# ---------------------------------------------------------------------------
# Scenario `tran_sweeps` (Layer 5a)
# ---------------------------------------------------------------------------

@dataclass
class TranResult:
    sweep_name: str
    time_s: float
    output_node: str
    expected_v: float
    tolerance_v: float
    actual_v: Optional[float]
    status: str
    rationale: str = ""


def _nearest_time_sample(samples: list[tuple[float, float]], target_s: float
                          ) -> Optional[tuple[float, float]]:
    if not samples:
        return None
    return min(samples, key=lambda s: abs(s[0] - target_s))


def evaluate_tran_sweep(sweep_name: str, sweep: dict,
                         samples: dict[str, list[tuple[float, float]]]
                         ) -> list[TranResult]:
    results: list[TranResult] = []
    default_out = sweep.get("output_node")
    for p in sweep.get("expected", []):
        out_node_raw = p.get("output_node", default_out)
        if out_node_raw is None:
            continue
        out_node = spice_node(out_node_raw).lower()
        t = p["time_s"]
        expected_v = p["voltage_v"]
        tol = p.get("tolerance_v", 0.05)
        rationale = p.get("rationale", "")
        block = samples.get(out_node)
        if not block:
            results.append(TranResult(sweep_name, t, out_node_raw, expected_v,
                                       tol, None, "missing", rationale))
            continue
        nearest = _nearest_time_sample(block, t)
        if nearest is None:
            results.append(TranResult(sweep_name, t, out_node_raw, expected_v,
                                       tol, None, "missing", rationale))
            continue
        _, actual_v = nearest
        status = "pass" if abs(actual_v - expected_v) <= tol else "fail"
        results.append(TranResult(sweep_name, t, out_node_raw, expected_v,
                                   tol, actual_v, status, rationale))
    return results
