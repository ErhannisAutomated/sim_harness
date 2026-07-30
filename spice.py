"""SPICE deck generation, ngspice invocation, and raw output parsing.

Extracted from dc_op_check.py to keep that CLI thin. Nothing here evaluates
scenario expectations or dc_checks — that's checks.py's job. This module
is purely:

  - SPICE syntax generation (node/ref sanitization, .model directives,
    2-terminal emission, semiconductor emission, IC dc_model emission)
  - build_spice_deck (main entry) — assembles a full deck for either
    a DC operating-point analysis or an AC sweep
  - run_ngspice — subprocess wrapper with TMPDIR-set-for-sandbox
  - parse_node_voltages / parse_ac_output — raw output text -> dicts
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from static_check import Comp, Netlist, match_spec, parse_value


# ---------------------------------------------------------------------------
# Component-part categorization for the DC/AC pass
# ---------------------------------------------------------------------------

_RESISTIVE_PARTS = {"R", "Thermistor_NTC", "Thermistor_PTC", "Thermistor"}
_INDUCTIVE_PARTS = {"L", "FerriteBead"}
_CAPACITIVE_PARTS = {"C"}
_DIODE_PARTS = {"D", "D_TVS", "LED", "D_Schottky", "D_Zener"}
_NMOS_PARTS = {"Q_NMOS", "Q_NMOS_DGS", "Q_NMOS_GDS", "Q_NMOS_GSD"}
_PMOS_PARTS = {"Q_PMOS", "Q_PMOS_DGS", "Q_PMOS_GDS", "Q_PMOS_GSD"}
_SKIP_PARTS_PREFIX = ("BH-", "USB_", "Conn_", "SW_", "Schematic_Metadata", "SRV05")

STABILIZER_R_OHMS = 1e9   # 1 GΩ to GND on every node — floating-net safety net
SHORT_OHMS = 1e-6         # µΩ stand-in for ferrites / unparseable inductors


def dc_kind(comp: Comp) -> str:
    """'resistive' | 'inductive' | 'capacitive' | 'diode' | 'nmos' | 'pmos'
    | 'ic' | 'skip'."""
    p = (comp.libsource_part or "").strip()
    if p in _RESISTIVE_PARTS: return "resistive"
    if p in _INDUCTIVE_PARTS: return "inductive"
    if p in _CAPACITIVE_PARTS: return "capacitive"
    if p in _DIODE_PARTS: return "diode"
    if p in _NMOS_PARTS: return "nmos"
    if p in _PMOS_PARTS: return "pmos"
    if any(p.startswith(prefix) for prefix in _SKIP_PARTS_PREFIX): return "skip"
    return "ic"


# ---------------------------------------------------------------------------
# SPICE node / reference naming
# ---------------------------------------------------------------------------

_GND_RE = re.compile(r"^(GND|AGND|PGND|DGND|VSS)$")


def is_ground_net(net_name: str) -> bool:
    return bool(_GND_RE.fullmatch(net_name.rsplit("/", 1)[-1]))


def spice_node(net_name: str) -> str:
    """Map a schematic net name to a SPICE node identifier. GND → 0."""
    if is_ground_net(net_name):
        return "0"
    return "n_" + re.sub(r"[^a-zA-Z0-9]", "_", net_name.lstrip("/"))


def spice_ref(ref: str) -> str:
    """Make a component reference safe for SPICE element naming."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", ref)


def pin_net_lookup(comp_ref: str, pin_indices: list, nl: Netlist
                   ) -> Optional[tuple[str, str]]:
    """Resolve an aliased pin index list to (matched_idx, net_name).
    Returns None if none of the candidates resolve to a net in the netlist."""
    for idx in pin_indices:
        s = str(idx)
        if (comp_ref, s) in nl.pin_net:
            return s, nl.pin_net[(comp_ref, s)]
    return None


# ---------------------------------------------------------------------------
# Semiconductor .model library (Layer 3c)
# ---------------------------------------------------------------------------

# Generic-but-reasonable parameters — V_F ≈ 0.7V for D_GENERIC, V_TH ≈ 1.5V
# for NMOS_GENERIC. Per-component vendor overrides deferred.
_MODEL_LIBRARY = {
    "diode": ".model D_GENERIC D IS=1e-14 N=1 RS=0.01 BV=100",
    "nmos":  ".model NMOS_GENERIC NMOS VTO=1.5 KP=100u LAMBDA=0.01",
    "pmos":  ".model PMOS_GENERIC PMOS VTO=-1.5 KP=50u LAMBDA=0.01",
}


# ---------------------------------------------------------------------------
# Emission helpers
# ---------------------------------------------------------------------------

def _emit_two_terminal(lines: list, comp: Comp, nl: Netlist,
                        spice_letter: str, value: float, used: set[str]):
    """Emit a 2-pin SPICE element (assumes the KiCad symbol has pins '1' and '2')."""
    n1 = nl.pin_net.get((comp.ref, "1"))
    n2 = nl.pin_net.get((comp.ref, "2"))
    if n1 is None or n2 is None:
        print(f"warning: {comp.ref} has non-standard pin numbering; skipped",
              file=sys.stderr)
        return
    a, b = spice_node(n1), spice_node(n2)
    used.add(a); used.add(b)
    lines.append(f"{spice_letter}{spice_ref(comp.ref)} {a} {b} {value:g}")


def _emit_diode(lines: list, comp: Comp, nl: Netlist, used: set[str]) -> bool:
    """KiCad Device library: pin 1 = K, pin 2 = A."""
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
    """Q_NMOS/Q_PMOS symbols expose G/D/S pin names; body tied to source.
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
    # L=1u W=1m → W/L=1000; R_DS(on) ≈ 6Ω at V_GS-V_TH=1.5V (switch-like).
    lines.append(f"M{spice_ref(comp.ref)} {dn} {gn} {sn} {sn} {model} L=1u W=1m")
    return True


def _emit_norton_pin(lines: list, comp_ref: str, matched_idx: str,
                     node: str, dc: dict):
    """SPICE current source: `I<name> N+ N-` sends positive current + → -
    through the source (externally: current LEAVES the - terminal).
    out_of_pin → pin at -; into_pin → pin at +."""
    ic_tag = f"{spice_ref(comp_ref)}_{spice_ref(matched_idx)}"
    current = dc["current_a"]
    if dc["direction"] == "out_of_pin":
        lines.append(f"I_ic_{ic_tag} 0 {node} DC {current:g}")
    else:
        lines.append(f"I_ic_{ic_tag} {node} 0 DC {current:g}")


def _emit_ic_dc_models(lines: list, comp: Comp, spec: dict, nl: Netlist,
                        rail_class_map: dict, used: set[str]):
    """Walk the IC's pins; for each with a dc_model, emit its SPICE contribution."""
    for pin in spec.get("pins", []):
        dc = pin.get("dc_model")
        if dc is None or dc.get("kind") == "high_z":
            continue
        idx_candidates = pin["index"] if isinstance(pin["index"], list) else [pin["index"]]
        resolved = pin_net_lookup(comp.ref, idx_candidates, nl)
        if resolved is None:
            continue
        matched_idx, net = resolved
        node = spice_node(net)
        used.add(node)

        if dc.get("kind") == "sourced":
            _emit_norton_pin(lines, comp.ref, matched_idx, node, dc)
            continue

        # kind == "driven"
        if "voltage_v" in dc:
            voltage = dc["voltage_v"]
        elif "follows_rail" in dc:
            # Rail-following: the scenario's rail source drives the node
            # (or the class is unmapped, in which case fall back to high_z).
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


# ---------------------------------------------------------------------------
# Main entry: build the full SPICE deck
# ---------------------------------------------------------------------------

def build_spice_deck(nl: Netlist, specs: dict, scenario: dict,
                      ac_sweep: Optional[dict] = None,
                      ac_output_nodes: Optional[list[str]] = None
                      ) -> tuple[str, set[str]]:
    """Return (deck_text, spice_nodes_used).

    When ac_sweep is provided, produces an AC deck: the source_node's
    voltage source becomes DC+AC, and a .control block runs `ac` instead
    of `op` and prints magnitude/phase at ac_output_nodes.
    """
    lines: list[str] = [
        f"* auto-generated by sim_harness/dc_op_check.py",
        f"* scenario: {scenario['name']}",
        "",
    ]
    used_nodes: set[str] = set()
    rail_class_map = scenario.get("rail_class_map", {})
    ac_source_node = spice_node(ac_sweep["source_node"]) if ac_sweep else None

    # 1) power rails: ideal voltage sources (add AC=1 on the sweep source)
    for rail, voltage in scenario["power_rails"].items():
        sn = spice_node(rail)
        used_nodes.add(sn)
        ac_tag = " AC 1" if sn == ac_source_node else ""
        lines.append(f"V_rail_{spice_ref(rail)} {sn} 0 DC {voltage}{ac_tag}")

    # 1b) AC source at a node not declared as a rail → dc=0, ac=1
    if ac_sweep is not None:
        rail_nodes = {spice_node(r) for r in scenario["power_rails"]}
        if ac_source_node not in rail_nodes:
            used_nodes.add(ac_source_node)
            lines.append(f"V_ac_only_{spice_ref(ac_sweep['source_node'])} "
                          f"{ac_source_node} 0 DC 0 AC 1")

    # 2) walk every component, emit appropriate SPICE element(s)
    device_kinds_used: set[str] = set()
    for comp in nl.comps.values():
        kind = dc_kind(comp)
        if kind == "skip":
            continue
        if kind == "resistive":
            _emit_two_terminal(lines, comp, nl, "R",
                                parse_value(comp.value) or 0.0, used_nodes)
        elif kind == "capacitive":
            val = parse_value(comp.value)
            if val is not None:
                _emit_two_terminal(lines, comp, nl, "C", val, used_nodes)
        elif kind == "inductive":
            val = parse_value(comp.value)
            if val is not None and comp.libsource_part == "L":
                _emit_two_terminal(lines, comp, nl, "L", val, used_nodes)
            else:  # FerriteBead / unparseable L → DC-only short
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

    # 2b) .model directives for whichever device kinds we emitted
    lines.append("")
    for dk in sorted(device_kinds_used):
        lines.append(_MODEL_LIBRARY[dk])

    # 3) 1 GΩ stabilizer on every non-GND node
    for node in sorted(used_nodes):
        if node == "0":
            continue
        lines.append(f"R_stab_{node} {node} 0 {STABILIZER_R_OHMS}")

    # 4) control block: .op or .ac
    lines.append("")
    lines.append(".control")
    if ac_sweep is None:
        lines.extend(["op", "let out = 0", "print all"])
    else:
        sw = ac_sweep["sweep"]
        kind = {"decade": "dec", "octave": "oct", "linear": "lin"}[sw["kind"]]
        n = sw.get("n_points", 100) if sw["kind"] == "linear" else sw.get("points_per_decade", 10)
        lines.append(f"ac {kind} {n} {sw['start_hz']} {sw['stop_hz']}")
        for out_node in (ac_output_nodes or []):
            lines.append(f"print v({out_node})")
    lines.extend([".endc", ".end"])
    return "\n".join(lines) + "\n", used_nodes


# ---------------------------------------------------------------------------
# ngspice invocation
# ---------------------------------------------------------------------------

def run_ngspice(deck_path: Path, work_dir: Path) -> str:
    """Run ngspice -b on the deck, return combined stdout+stderr.
    Sets TMPDIR to work_dir so ngspice's internal glibc tmpfile() writes
    land somewhere the sandbox permits (default /tmp is blocked)."""
    env = os.environ.copy()
    env["TMPDIR"] = str(work_dir)
    proc = subprocess.run(
        ["ngspice", "-b", str(deck_path)],
        capture_output=True, text=True, timeout=60, env=env,
    )
    return proc.stdout + "\n---STDERR---\n" + proc.stderr


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------

_V_RE = re.compile(
    r"^\s*(v\([^)]+\)|[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)


def parse_node_voltages(ngspice_output: str) -> dict[str, float]:
    """Extract 'node = voltage' pairs from ngspice's .op print output.
    Handles both bare-name (`n_buckboost_fb = ...`) and V(...)-wrapped forms."""
    voltages: dict[str, float] = {}
    for line in ngspice_output.splitlines():
        m = _V_RE.match(line)
        if not m:
            continue
        raw = m.group(1)
        val = float(m.group(2))
        name = raw.lower()
        vm = re.fullmatch(r"v\(([^)]+)\)", name)
        if vm:
            name = vm.group(1)
        voltages[name] = val
    return voltages


# ngspice AC output: `<idx>\t<freq>\t<real>,\t<imag>\t`
_AC_LINE_RE = re.compile(
    r"^\s*\d+\s+(-?\d+\.\d+e[+-]?\d+)\s+"
    r"(-?\d+\.\d+e[+-]?\d+)\s*,\s*"
    r"(-?\d+\.\d+e[+-]?\d+)"
)


def parse_ac_output(text: str) -> dict[str, list[tuple[float, complex]]]:
    """Extract per-node frequency/complex-voltage samples from ngspice AC output.
    Returns {output_node: [(freq_hz, complex_value), ...]}. Each `print v(node)`
    produces one column-block preceded by a header line naming the node."""
    blocks: dict[str, list[tuple[float, complex]]] = {}
    current_node: Optional[str] = None
    header_re = re.compile(r"^\s*Index\s+frequency\s+(v\([^)]+\))\s*$",
                           re.IGNORECASE)
    for line in text.splitlines():
        h = header_re.match(line)
        if h:
            m = re.fullmatch(r"v\(([^)]+)\)", h.group(1), re.IGNORECASE)
            current_node = m.group(1).lower() if m else None
            if current_node is not None:
                blocks.setdefault(current_node, [])
            continue
        if current_node is None:
            continue
        m = _AC_LINE_RE.match(line)
        if not m:
            continue
        freq = float(m.group(1))
        real = float(m.group(2))
        imag = float(m.group(3))
        blocks[current_node].append((freq, complex(real, imag)))
    return blocks


def output_nodes_for_sweep(sweep: dict) -> list[str]:
    """Union of the sweep-level output_node and any per-expected-point overrides."""
    default = sweep.get("output_node")
    per_point = [p.get("output_node") for p in sweep.get("expected", [])]
    all_nodes = {default} | set(per_point)
    all_nodes.discard(None)
    return sorted(all_nodes)
