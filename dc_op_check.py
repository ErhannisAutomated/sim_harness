#!/usr/bin/env python3
"""Layer-3a DC operating-point checker.

Given a KiCad schematic + a scenario JSON (declared power rails and expected
node voltages) + the component-spec directory, this:

  1. Builds a SPICE netlist covering the LINEAR SUBNETWORK of the schematic
     (resistors, ferrite beads → 0 Ω, thermistors → their rated R, inductors
     → 0 Ω, caps → open). Semiconductors and connectors are skipped.
  2. Emits Thevenin sources for IC pins with `dc_model` in their spec.
  3. Emits ideal voltage sources for scenario-declared power rails.
  4. Adds a 1 GΩ-to-GND stabilizer on every non-GND node (kills floating-node
     singularities without measurably perturbing real biasing).
  5. Runs ngspice in batch mode, parses node voltages from the operating-point
     output, compares to the scenario's `expected` values, reports pass/fail.

Exit code 0 iff every expected node is within tolerance.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Layer 1 already handles netlist + spec loading; reuse it.
sys.path.insert(0, str(Path(__file__).parent))
from static_check import (  # noqa: E402
    Comp, Netlist, load_netlist, load_specs, match_spec, parse_value,
)


SCENARIO_SCHEMA_PATH = (
    Path(__file__).parent / "schema" / "scenario.v0.schema.json"
)

# Libsource parts we treat as DC resistive elements (value parsed for R).
_RESISTIVE_PARTS = {"R", "Thermistor_NTC", "Thermistor_PTC", "Thermistor"}
# Parts that collapse to a DC short (µΩ resistor).
_INDUCTIVE_PARTS = {"L", "FerriteBead"}
# Capacitors are opens (skip entirely).
_CAPACITIVE_PARTS = {"C"}
# Two-terminal diode-like parts (pin 1 = K/cathode, pin 2 = A/anode per KiCad
# Device library convention). D_TVS bidirectional TVS is modeled as a
# unidirectional diode — accurate enough for DC-off analysis (leakage tiny
# either way).
_DIODE_PARTS = {"D", "D_TVS", "LED", "D_Schottky", "D_Zener"}
# N-channel MOSFETs — G/D/S pin identifiers per KiCad Q_NMOS symbol.
_NMOS_PARTS = {"Q_NMOS", "Q_NMOS_DGS", "Q_NMOS_GDS", "Q_NMOS_GSD"}
_PMOS_PARTS = {"Q_PMOS", "Q_PMOS_DGS", "Q_PMOS_GDS", "Q_PMOS_GSD"}
# Parts to skip in DC analysis (mechanical, metadata, multi-pin protection ICs).
# SRV05-4 is a multi-diode ESD array — reverse-biased under normal operation,
# so omitting doesn't perturb the DC solve; adding proper diode topology
# per-pin is future work.
_SKIP_PARTS_PREFIX = ("BH-", "USB_", "Conn_", "SW_", "Schematic_Metadata", "SRV05")

STABILIZER_R_OHMS = 1e9      # 1 GΩ to GND on every node — floating-net safety net
SHORT_OHMS = 1e-6            # µΩ stand-in for inductors / ferrites (avoids R=0 singularities)


# --------------------------------------------------------------------------
# SPICE node naming
# --------------------------------------------------------------------------

_GND_RE = re.compile(r"^(GND|AGND|PGND|DGND|VSS)$")

def is_ground_net(net_name: str) -> bool:
    return bool(_GND_RE.fullmatch(net_name.rsplit("/", 1)[-1]))


def spice_node(net_name: str) -> str:
    """Map a schematic net name to a SPICE node identifier. GND → 0."""
    if is_ground_net(net_name):
        return "0"
    # Sanitize: replace anything not alphanumeric with underscore
    return "n_" + re.sub(r"[^a-zA-Z0-9]", "_", net_name.lstrip("/"))


def spice_ref(ref: str) -> str:
    """Make a component reference safe for SPICE element naming (strip odd chars)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", ref)


# --------------------------------------------------------------------------
# Component categorization for the DC pass
# --------------------------------------------------------------------------

def dc_kind(comp: Comp) -> str:
    """Categorize for DC: 'resistive' | 'inductive' | 'capacitive' |
    'diode' | 'nmos' | 'pmos' | 'ic' | 'skip'."""
    p = (comp.libsource_part or "").strip()
    if p in _RESISTIVE_PARTS: return "resistive"
    if p in _INDUCTIVE_PARTS: return "inductive"
    if p in _CAPACITIVE_PARTS: return "capacitive"
    if p in _DIODE_PARTS: return "diode"
    if p in _NMOS_PARTS: return "nmos"
    if p in _PMOS_PARTS: return "pmos"
    if any(p.startswith(prefix) for prefix in _SKIP_PARTS_PREFIX): return "skip"
    return "ic"


# --------------------------------------------------------------------------
# Scenario loading
# --------------------------------------------------------------------------

def load_scenario(path: Path) -> dict:
    import jsonschema
    schema = json.loads(SCENARIO_SCHEMA_PATH.read_text())
    scenario = json.loads(path.read_text())
    jsonschema.Draft202012Validator(schema).validate(scenario)
    return scenario


# --------------------------------------------------------------------------
# SPICE deck generation
# --------------------------------------------------------------------------

def _pin_net(comp_ref: str, pin_indices: list, nl: Netlist) -> Optional[tuple[str, str]]:
    """Return (matched_idx, net) for the first alias that resolves; None if none do."""
    for idx in pin_indices:
        s = str(idx)
        if (comp_ref, s) in nl.pin_net:
            return s, nl.pin_net[(comp_ref, s)]
    return None


def build_spice_deck(nl: Netlist, specs: dict, scenario: dict) -> tuple[str, set[str]]:
    """Return (deck_text, spice_nodes_used)."""
    lines: list[str] = [
        f"* auto-generated by sim_harness/dc_op_check.py",
        f"* scenario: {scenario['name']}",
        "",
    ]
    used_nodes: set[str] = set()
    rail_class_map = scenario.get("rail_class_map", {})

    def track_node(sn: str):
        used_nodes.add(sn)

    # 1) power rails: ideal voltage sources
    for rail, voltage in scenario["power_rails"].items():
        sn = spice_node(rail)
        track_node(sn)
        lines.append(f"V_rail_{spice_ref(rail)} {sn} 0 DC {voltage}")

    # 2) walk every component, emit appropriate SPICE element(s)
    device_kinds_used: set[str] = set()
    for comp in nl.comps.values():
        kind = dc_kind(comp)
        if kind == "capacitive" or kind == "skip":
            continue
        if kind == "resistive":
            _emit_two_terminal(lines, comp, nl, "R", parse_value(comp.value) or 0.0, used_nodes)
        elif kind == "inductive":
            _emit_two_terminal(lines, comp, nl, "R", SHORT_OHMS, used_nodes)
        elif kind == "diode":
            if _emit_diode(lines, comp, nl, used_nodes):
                device_kinds_used.add("diode")
        elif kind == "nmos":
            if _emit_mosfet(lines, comp, nl, used_nodes, "nmos"):
                device_kinds_used.add("nmos")
        elif kind == "pmos":
            if _emit_mosfet(lines, comp, nl, used_nodes, "pmos"):
                device_kinds_used.add("pmos")
        elif kind == "ic":
            spec_hit = match_spec(comp, specs)
            if spec_hit is None:
                continue
            _emit_ic_dc_models(lines, comp, spec_hit[1], nl, rail_class_map, used_nodes)

    # 2b) emit .model directives for any device kinds we used
    lines.append("")
    for dk in sorted(device_kinds_used):
        lines.append(_MODEL_LIBRARY[dk])

    # 3) 1 GΩ stabilizer on every non-GND node (kills floating-net singularities)
    for node in sorted(used_nodes):
        if node == "0":
            continue
        lines.append(f"R_stab_{node} {node} 0 {STABILIZER_R_OHMS}")

    # 4) control block: DC op point, print voltage at every node
    lines.extend([
        "",
        ".control",
        "op",
        "let out = 0",  # dummy — nothing captured; we parse the .op printout
        "print all",
        ".endc",
        ".end",
    ])
    return "\n".join(lines) + "\n", used_nodes


def _emit_two_terminal(lines: list, comp: Comp, nl: Netlist,
                        spice_letter: str, value: float, used: set[str]):
    """Emit a 2-pin SPICE element (assumes the KiCad symbol has pins '1' and '2')."""
    n1 = nl.pin_net.get((comp.ref, "1"))
    n2 = nl.pin_net.get((comp.ref, "2"))
    if n1 is None or n2 is None:
        # Unusual pin numbering — skip and warn to stderr
        print(f"warning: {comp.ref} has non-standard pin numbering; skipped", file=sys.stderr)
        return
    a, b = spice_node(n1), spice_node(n2)
    used.add(a); used.add(b)
    lines.append(f"{spice_letter}{spice_ref(comp.ref)} {a} {b} {value:g}")


# --------------------------------------------------------------------------
# Layer-3c: semiconductor emission + built-in device model library
# --------------------------------------------------------------------------

# Model library. Parameters are generic-but-reasonable — chosen to give
# canonical DC behavior (V_F ~ 0.7V for D_GENERIC, V_TH ~ 1.5V for
# NMOS_GENERIC) with adequate numerical stability for ngspice's Newton
# solver. Real vendor models can override per-component via a
# `semiconductor_model` field in the spec (not implemented in v0).
_MODEL_LIBRARY = {
    "diode": ".model D_GENERIC D IS=1e-14 N=1 RS=0.01 BV=100",
    "nmos":  ".model NMOS_GENERIC NMOS VTO=1.5 KP=100u LAMBDA=0.01",
    "pmos":  ".model PMOS_GENERIC PMOS VTO=-1.5 KP=50u LAMBDA=0.01",
}


def _emit_diode(lines: list, comp: Comp, nl: Netlist, used: set[str]) -> bool:
    """KiCad Device library convention: pin 1 = K (cathode), pin 2 = A (anode).
    Returns True if emitted, False if pin lookup failed."""
    k = nl.pin_net.get((comp.ref, "1"))
    a = nl.pin_net.get((comp.ref, "2"))
    if k is None or a is None:
        print(f"warning: diode {comp.ref} has non-standard pin numbering; skipped",
              file=sys.stderr)
        return False
    anode_n, cathode_n = spice_node(a), spice_node(k)
    used.add(anode_n); used.add(cathode_n)
    lines.append(f"D{spice_ref(comp.ref)} {anode_n} {cathode_n} D_GENERIC")
    return True


def _emit_mosfet(lines: list, comp: Comp, nl: Netlist,
                  used: set[str], polarity: str) -> bool:
    """KiCad Q_NMOS/Q_PMOS symbols expose pins by name: G / D / S. Bulk (body)
    is not exposed at the schematic level; we tie it to source (standard for
    discrete power FETs where body = source internally).
    SPICE syntax: M<name> <drain> <gate> <source> <bulk> <model>."""
    g = nl.pin_net.get((comp.ref, "G"))
    d = nl.pin_net.get((comp.ref, "D"))
    s = nl.pin_net.get((comp.ref, "S"))
    if g is None or d is None or s is None:
        print(f"warning: {polarity} MOSFET {comp.ref} missing G/D/S pin; skipped",
              file=sys.stderr)
        return False
    dn, gn, sn = spice_node(d), spice_node(g), spice_node(s)
    used.add(dn); used.add(gn); used.add(sn)
    model = "NMOS_GENERIC" if polarity == "nmos" else "PMOS_GENERIC"
    # L=1u W=1m → W/L=1000; with KP=100µA/V² this gives R_DS(on) ≈ 6Ω at
    # V_GS-V_TH=1.5V — enough to behave switch-like in a DC solve.
    lines.append(f"M{spice_ref(comp.ref)} {dn} {gn} {sn} {sn} {model} L=1u W=1m")
    return True


def _emit_ic_dc_models(lines: list, comp: Comp, spec: dict, nl: Netlist,
                        rail_class_map: dict, used: set[str]):
    """Walk the IC's pins; for each with a dc_model, emit the SPICE contribution."""
    for pin in spec.get("pins", []):
        dc = pin.get("dc_model")
        if dc is None or dc.get("kind") == "high_z":
            continue
        # Resolve which schematic pin this is on (respecting aliases)
        idx_candidates = pin["index"] if isinstance(pin["index"], list) else [pin["index"]]
        resolved = _pin_net(comp.ref, idx_candidates, nl)
        if resolved is None:
            continue
        matched_idx, net = resolved
        node = spice_node(net)
        used.add(node)
        if dc.get("kind") == "sourced":
            _emit_norton_pin(lines, comp.ref, matched_idx, node, dc)
            continue
        # kind == "driven" — fall through to the Thevenin block below

        if "voltage_v" in dc:
            voltage = dc["voltage_v"]
        elif "follows_rail" in dc:
            # Rail-following pin: the scenario has already emitted a voltage
            # source for the rail (or the rail is unmapped, in which case the
            # pin falls back to high_z-equivalent).  We assume the pin is on
            # the same schematic net as the rail (the usual pattern) and let
            # the rail's own source drive the node — nothing to emit here.
            continue
        else:
            continue

        z = dc.get("impedance_ohms", 0.0)
        ic_tag = f"{spice_ref(comp.ref)}_{spice_ref(matched_idx)}"
        if z <= 0:
            lines.append(f"V_ic_{ic_tag} {node} 0 DC {voltage:g}")
        else:
            internal = f"n_thev_{ic_tag}"
            lines.append(f"V_ic_{ic_tag} {internal} 0 DC {voltage:g}")
            lines.append(f"R_ic_{ic_tag} {internal} {node} {z:g}")
            used.add(internal)


def _emit_norton_pin(lines: list, comp_ref: str, matched_idx: str,
                     node: str, dc: dict):
    """SPICE: `I<name> N+ N-` sends positive current from + through the source
    to − (externally: current LEAVES the − terminal). So to model a pin that
    sources current OUT of the pin, put the pin at the − terminal
    (`I ... 0 pin_node`). To sink current INTO the pin, put the pin at + (`I
    ... pin_node 0`)."""
    ic_tag = f"{spice_ref(comp_ref)}_{spice_ref(matched_idx)}"
    current = dc["current_a"]
    if dc["direction"] == "out_of_pin":
        lines.append(f"I_ic_{ic_tag} 0 {node} DC {current:g}")
    else:  # into_pin
        lines.append(f"I_ic_{ic_tag} {node} 0 DC {current:g}")


# --------------------------------------------------------------------------
# ngspice invocation + output parsing
# --------------------------------------------------------------------------

def run_ngspice(deck_path: Path, work_dir: Path) -> str:
    """Run ngspice -b on the deck file, return combined stdout+stderr.
    Sets TMPDIR to work_dir so ngspice's internal tmpfile() writes land
    somewhere writable (the sandbox blocks /tmp)."""
    import os
    env = os.environ.copy()
    env["TMPDIR"] = str(work_dir)
    proc = subprocess.run(
        ["ngspice", "-b", str(deck_path)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    return proc.stdout + "\n---STDERR---\n" + proc.stderr


_V_RE = re.compile(r"^\s*(v\([^)]+\)|[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")

def parse_node_voltages(ngspice_output: str) -> dict[str, float]:
    """Extract 'node = voltage' pairs from ngspice's .op print output.

    ngspice's `print all` inside .control emits lines like:
        n_buckboost_fb = 1.189000e+00
    (all lowercase). Some versions instead print `V(n_buckboost_fb) = ...`.
    Handle both.
    """
    voltages: dict[str, float] = {}
    for line in ngspice_output.splitlines():
        m = _V_RE.match(line)
        if not m:
            continue
        raw = m.group(1)
        val = float(m.group(2))
        # Strip V(...) wrapper if present, lowercase, drop leading n_
        name = raw.lower()
        vm = re.fullmatch(r"v\(([^)]+)\)", name)
        if vm:
            name = vm.group(1)
        voltages[name] = val
    return voltages


# --------------------------------------------------------------------------
# Comparison + reporting
# --------------------------------------------------------------------------

@dataclass
class Result:
    node: str
    expected_v: float
    tolerance_v: float
    actual_v: Optional[float]
    status: str        # 'pass' | 'fail' | 'missing'


def compare_expected(voltages: dict[str, float], scenario: dict) -> list[Result]:
    results = []
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


@dataclass
class CheckResult:
    ic_ref: str
    pin: str
    pin_name: str
    check_kind: str
    description: str      # e.g. "must equal 1.2V ±0.02"
    actual_v: Optional[float]
    status: str           # 'pass' | 'fail' | 'missing'
    rationale: str = ""


def evaluate_dc_checks(nl: Netlist, specs: dict,
                       voltages: dict[str, float]) -> list[CheckResult]:
    """Evaluate every pin-level dc_check across the design. Returns a list
    of CheckResults; one per (component × pin × check)."""
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
            resolved = _pin_net(comp.ref, [str(c) for c in candidates], nl)
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


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenario", type=Path, help="path to scenario JSON")
    ap.add_argument("schematic", type=Path, nargs="?",
                    help="path to schematic (falls back to netlist arg if omitted)")
    ap.add_argument("--netlist", type=Path,
                    help="pre-exported kicadxml netlist; skips kicad-cli invocation")
    ap.add_argument("--components-dir", type=Path, default=Path("components"))
    ap.add_argument("--work-dir", type=Path,
                    default=Path(os.environ.get("TMPDIR", "/tmp/claude-1000")))
    ap.add_argument("--keep-deck", action="store_true",
                    help="don't delete the generated SPICE deck (useful for debugging)")
    args = ap.parse_args()

    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.netlist:
        xml_path = args.netlist
    elif args.schematic:
        xml_path = args.work_dir / (args.schematic.stem + ".net.xml")
        subprocess.run(["kicad-cli", "sch", "export", "netlist", "--format",
                        "kicadxml", "-o", str(xml_path), str(args.schematic)],
                       check=True)
    else:
        print("error: need either --netlist or a schematic path", file=sys.stderr)
        return 2

    nl = load_netlist(xml_path)
    specs = load_specs(args.components_dir)
    scenario = load_scenario(args.scenario)

    deck_text, _ = build_spice_deck(nl, specs, scenario)
    deck_path = args.work_dir / (scenario["name"] + ".cir")
    deck_path.write_text(deck_text)

    output = run_ngspice(deck_path, args.work_dir)
    voltages = parse_node_voltages(output)

    results = compare_expected(voltages, scenario)
    check_results = evaluate_dc_checks(nl, specs, voltages)

    if results:
        print("--- scenario `expected` checks ---")
        print(format_results(results))
    if check_results:
        print("--- component spec `dc_checks` ---")
        for r in check_results:
            tag = {"pass": "PASS   ", "fail": "FAIL   ", "missing": "MISSING"}[r.status]
            actual = f"{r.actual_v:+.4f} V" if r.actual_v is not None else "(no voltage)"
            print(f"[{tag}] {r.ic_ref} pin {r.pin} ({r.pin_name}): "
                  f"{actual} — {r.description}")
            if r.status == "fail" and r.rationale:
                print(f"          rationale: {r.rationale}")

    all_status = [r.status for r in results] + [r.status for r in check_results]
    n_pass = sum(1 for s in all_status if s == "pass")
    n_fail = sum(1 for s in all_status if s == "fail")
    n_miss = sum(1 for s in all_status if s == "missing")
    print(f"\n{n_pass} pass, {n_fail} fail, {n_miss} missing "
          f"(of {len(all_status)} checks)")

    if not args.keep_deck and deck_path.exists():
        if all(s == "pass" for s in all_status):
            deck_path.unlink()

    return 0 if all(s == "pass" for s in all_status) else 1


if __name__ == "__main__":
    sys.exit(main())
