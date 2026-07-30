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
