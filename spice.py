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

import math
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
                        rail_class_map: dict, used: set[str],
                        skip_pin_indices: Optional[set[str]] = None):
    """Walk the IC's pins; for each with a dc_model, emit its SPICE contribution.

    `skip_pin_indices` — pins already covered by an `ac_model` or
    `behavioral_spice_subckt` on this IC. Their per-pin dc_model is
    suppressed to avoid double-driving the same node."""
    skip = skip_pin_indices or set()
    for pin in spec.get("pins", []):
        dc = pin.get("dc_model")
        if dc is None or dc.get("kind") == "high_z":
            continue
        idx_candidates = pin["index"] if isinstance(pin["index"], list) else [pin["index"]]
        resolved = pin_net_lookup(comp.ref, idx_candidates, nl)
        if resolved is None:
            continue
        matched_idx, net = resolved
        if matched_idx in skip:
            continue
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
# Layer-5c: pin-level transient_model emission
# ---------------------------------------------------------------------------

def _emit_transient_model(lines: list, comp: Comp, spec: dict, pin: dict,
                           matched_idx: str, node: str, nl: Netlist,
                           used: set[str]) -> bool:
    """Emit ngspice element(s) for a single pin's `transient_model`.

    Returns True on success. All topologies emit as B-elements (behavioral
    sources); some also emit RC networks for filtering. Only invoked when
    the runner is building a .tran deck (transient_model is time-domain-
    only; for .op and .ac the dc_model / ac_model paths apply)."""
    tm = pin["transient_model"]
    topo = tm["topology"]
    tag = f"{spice_ref(comp.ref)}_{spice_ref(matched_idx)}"

    if topo == "current_source_with_clamp":
        # I = current_a * u(clamp_v - V(pin))
        # Runner sets `uic` on the tran directive when transient_models are
        # present — starts cap at 0V, source drives cap toward clamp, then
        # u() drops to 0 once clamp is reached. Without uic, ngspice's DC
        # op-point would find V(pin)=clamp_v as the steady state (source
        # off) and tran would start there, missing the ramp entirely.
        clamp = tm["clamp_v"]
        current = tm["current_a"]
        if tm["direction"] == "out_of_pin":
            lines.append(f"B_tm_{tag} 0 {node} I = "
                          f"{current:g} * u({clamp:g} - V({node}))")
        else:
            lines.append(f"B_tm_{tag} {node} 0 I = "
                          f"{current:g} * u(V({node}) - {clamp:g})")
        return True

    if topo == "voltage_after_delay":
        before = tm["before_v"]
        after = tm["after_v"]
        delay = tm["delay_s"]
        rise = tm.get("rise_time_s", 1e-9)
        t0 = max(0.0, delay - rise / 2)
        t1 = delay + rise / 2
        lines.append(f"V_tm_{tag} {node} 0 PWL("
                      f"0 {before:g} "
                      f"{t0:g} {before:g} "
                      f"{t1:g} {after:g})")
        return True

    if topo == "voltage_gated_by_input":
        sense_ref = tm["sense_pin"]
        # Resolve sense pin on same component
        sense_pin = _find_pin_by_index_ref(spec, sense_ref)
        if sense_pin is None:
            print(f"warning: {comp.ref} transient_model sense_pin {sense_ref!r} "
                  f"not found in spec; skipping", file=sys.stderr)
            return False
        idx_field = sense_pin["index"]
        candidates = idx_field if isinstance(idx_field, list) else [idx_field]
        resolved = pin_net_lookup(comp.ref, [str(c) for c in candidates], nl)
        if resolved is None:
            print(f"warning: {comp.ref} transient_model sense_pin {sense_ref!r} "
                  f"unresolved in netlist; skipping", file=sys.stderr)
            return False
        sense_node = spice_node(resolved[1])
        used.add(sense_node)

        threshold = tm["threshold_v"]
        low = tm["low_v"]
        high = tm["high_v"]
        stable = tm.get("stable_for_s", 0.0)
        if stable > 0:
            # Low-pass sense voltage through a G+R+C: τ = R*C = 1·stable_for_s
            filt = f"n_tm_filt_{tag}"
            used.add(filt)
            lines.append(f"G_tm_{tag} 0 {filt} {sense_node} 0 1")
            lines.append(f"R_tm_{tag} {filt} 0 1")
            lines.append(f"C_tm_{tag} {filt} 0 {stable:g}")
            probe = filt
        else:
            probe = sense_node
        # V = low + (high-low) * u(V(probe) - threshold)
        lines.append(f"B_tm_{tag} {node} 0 V = "
                      f"{low:g} + ({high:g} - {low:g}) * u(V({probe}) - {threshold:g})")
        return True

    print(f"warning: unknown transient_model topology {topo!r} on {comp.ref} pin {matched_idx}",
          file=sys.stderr)
    return False


def _emit_ic_transient_models(lines: list, comp: Comp, spec: dict, nl: Netlist,
                                used: set[str]) -> set[str]:
    """Walk the IC's pins; for each with a transient_model, emit its SPICE
    contribution. Returns the set of matched_idx values that were covered
    (so per-pin dc_model on those pins can be suppressed for this run)."""
    covered: set[str] = set()
    for pin in spec.get("pins", []):
        tm = pin.get("transient_model")
        if tm is None:
            continue
        idx_candidates = pin["index"] if isinstance(pin["index"], list) else [pin["index"]]
        resolved = pin_net_lookup(comp.ref, [str(c) for c in idx_candidates], nl)
        if resolved is None:
            continue
        matched_idx, net = resolved
        node = spice_node(net)
        used.add(node)
        if _emit_transient_model(lines, comp, spec, pin, matched_idx, node, nl, used):
            covered.add(matched_idx)
    return covered


# ---------------------------------------------------------------------------
# Behavioral IC models: ac_model (parametric) + behavioral_spice_subckt (vendor)
# ---------------------------------------------------------------------------

def _find_pin_by_index_ref(spec: dict, ref) -> Optional[dict]:
    """Find the spec pin whose `index` matches `ref`. Honours aliases: if the
    pin's `index` is an array, ref may match any element. `ref` may be int or
    string (compared as strings)."""
    want = str(ref)
    for pin in spec.get("pins", []):
        idx = pin.get("index")
        if isinstance(idx, list):
            if any(str(x) == want for x in idx):
                return pin
        elif str(idx) == want:
            return pin
    return None


def _resolve_role_net(comp_ref: str, spec: dict, ref, nl: Netlist
                       ) -> Optional[tuple[str, str]]:
    """Given an ac_model / subckt terminal reference to a pin, look up the
    corresponding net in the schematic. Returns (matched_idx, net) or None
    if the pin can't be found in the spec or the netlist."""
    pin = _find_pin_by_index_ref(spec, ref)
    if pin is None:
        return None
    idx_field = pin["index"]
    candidates = idx_field if isinstance(idx_field, list) else [idx_field]
    return pin_net_lookup(comp_ref, candidates, nl)


def _op_amp_single_pole_subckt(name: str, a0_db: float, gbw_hz: float,
                                output_z_ohm: float) -> list[str]:
    """Emit a canonical single-pole op-amp .subckt with the numbers baked in.

    Topology:
        E1  int 0 inp inm A0        ; ideal diff-amp × A0
        R1  int mid 1               ; RC low-pass; R=1, C set so f_p = GBW/A0
        C1  mid 0 <C>
        E2  buf 0 mid 0 1           ; ideal unity buffer
        Rout buf out ROUT           ; open-loop output impedance

    At DC: V(out) = A0 · (V(inp) − V(inm)) − I(load)·ROUT
    At AC: single pole at f_p = GBW/A0, so |gain| crosses 0 dB at f = GBW."""
    a0 = 10 ** (a0_db / 20.0)
    fp = gbw_hz / a0
    c1 = 1.0 / (2 * math.pi * 1.0 * fp)   # R=1 baked in
    return [
        f".subckt {name} inp inm out",
        f"E1 int 0 inp inm {a0:g}",
        f"R1 int mid 1",
        f"C1 mid 0 {c1:g}",
        f"E2 buf 0 mid 0 1",
        f"Rout buf out {output_z_ohm:g}",
        f".ends",
    ]


def _emit_ac_model(subckt_lines: list, x_lines: list, comp: Comp, spec: dict,
                    ac_model: dict, nl: Netlist, used: set[str]
                    ) -> Optional[set[str]]:
    """Emit the .subckt + X-instance for a parametric ac_model.

    Appends the .subckt block to `subckt_lines` and the X-line to `x_lines`.
    Returns the set of pin_index strings the model covers (so per-pin
    `dc_model` emission can be suppressed on those pins), or None if any
    required role pin could not be resolved."""
    topology = ac_model.get("topology")
    if topology != "op_amp_single_pole":
        return None  # future topologies land here
    pins = ac_model["pins"]
    resolved: dict[str, tuple[str, str]] = {}
    for role in ("in_plus", "in_minus", "output"):
        got = _resolve_role_net(comp.ref, spec, pins[role], nl)
        if got is None:
            print(f"warning: {comp.ref} ac_model role {role!r} pin "
                  f"{pins[role]!r} unresolved; skipping ac_model",
                  file=sys.stderr)
            return None
        resolved[role] = got

    subckt_name = f"OPAMP_SP_{spice_ref(comp.ref)}"
    subckt_lines.extend(_op_amp_single_pole_subckt(
        subckt_name,
        a0_db=ac_model["a0_db"],
        gbw_hz=ac_model["gbw_hz"],
        output_z_ohm=ac_model.get("output_z_ohm", 100.0),
    ))
    n_inp = spice_node(resolved["in_plus"][1])
    n_inm = spice_node(resolved["in_minus"][1])
    n_out = spice_node(resolved["output"][1])
    used.update([n_inp, n_inm, n_out])
    x_lines.append(f"X{spice_ref(comp.ref)} {n_inp} {n_inm} {n_out} {subckt_name}")
    return {matched_idx for (matched_idx, _) in resolved.values()}


def _emit_xspice_model(a_lines: list, model_lines: list, comp: Comp,
                        spec: dict, xspice: dict, nl: Netlist,
                        used: set[str]) -> Optional[set[str]]:
    """Emit `.model` + `A<ref>` for an XSPICE code model.

    The .cm file itself must be loaded via `codemodel` command BEFORE
    the netlist is parsed — see `collect_xspice_cm_paths()`. This
    function just emits the netlist-side pieces.

    Returns the set of pin_index strings the model covers, or None on
    resolution failure."""
    model_type = xspice["model_type"]
    per_instance = f"{model_type}_{spice_ref(comp.ref)}"

    tokens: list[str] = []
    covered: set[str] = set()
    for i, conn in enumerate(xspice["connections"]):
        kind = conn["kind"]
        if kind == "differential":
            pos = _resolve_role_net(comp.ref, spec, conn["pos_pin"], nl)
            if pos is None:
                print(f"warning: {comp.ref} xspice_model connection {i} pos_pin "
                      f"{conn['pos_pin']!r} unresolved; skipping",
                      file=sys.stderr)
                return None
            covered.add(pos[0])
            pos_node = spice_node(pos[1])
            used.add(pos_node)
            if "neg_pin" in conn:
                neg = _resolve_role_net(comp.ref, spec, conn["neg_pin"], nl)
                if neg is None:
                    print(f"warning: {comp.ref} xspice_model connection {i} neg_pin "
                          f"{conn['neg_pin']!r} unresolved; skipping",
                          file=sys.stderr)
                    return None
                covered.add(neg[0])
                neg_node = spice_node(neg[1])
                used.add(neg_node)
            else:
                neg_node = "0"
            tokens.append(f"%vd({pos_node} {neg_node})")
        elif kind == "single":
            got = _resolve_role_net(comp.ref, spec, conn["pin"], nl)
            if got is None:
                print(f"warning: {comp.ref} xspice_model connection {i} pin "
                      f"{conn['pin']!r} unresolved; skipping",
                      file=sys.stderr)
                return None
            covered.add(got[0])
            node = spice_node(got[1])
            used.add(node)
            tokens.append(node)
        else:
            print(f"warning: {comp.ref} unknown xspice connection kind {kind!r}",
                  file=sys.stderr)
            return None

    a_lines.append(f"A{spice_ref(comp.ref)} {' '.join(tokens)} {per_instance}")

    param_pairs = " ".join(f"{k}={v}" for k, v in
                            (xspice.get("model_params") or {}).items())
    param_clause = f" ({param_pairs})" if param_pairs else ""
    model_lines.append(f".model {per_instance} {model_type}{param_clause}")
    return covered


def _emit_subckt_instance(include_paths: set, x_lines: list, comp: Comp,
                           spec: dict, spec_path: Path, subckt_ref: dict,
                           nl: Netlist, used: set[str]) -> Optional[set[str]]:
    """Emit `.include <path>` (deduped via include_paths) + X-instance for a
    vendor-supplied .subckt. Returns the pin-index set covered, or None if
    any terminal fails to resolve."""
    path = (spec_path.parent / subckt_ref["path"]).resolve()
    resolved: list[tuple[str, tuple[str, str]]] = []
    for term in subckt_ref["terminals"]:
        got = _resolve_role_net(comp.ref, spec, term["pin_index"], nl)
        if got is None:
            print(f"warning: {comp.ref} subckt terminal {term['subckt_terminal']!r} "
                  f"→ pin_index {term['pin_index']!r} unresolved; skipping subckt",
                  file=sys.stderr)
            return None
        resolved.append((term["subckt_terminal"], got))
    include_paths.add(str(path))
    node_tokens: list[str] = []
    covered: set[str] = set()
    for _, (matched_idx, net) in resolved:
        node = spice_node(net)
        used.add(node)
        node_tokens.append(node)
        covered.add(matched_idx)
    x_lines.append(f"X{spice_ref(comp.ref)} {' '.join(node_tokens)} "
                    f"{subckt_ref['subckt_name']}")
    return covered


# ---------------------------------------------------------------------------
# Main entry: build the full SPICE deck
# ---------------------------------------------------------------------------

def build_spice_deck(nl: Netlist, specs: dict, scenario: dict,
                      ac_sweep: Optional[dict] = None,
                      ac_output_nodes: Optional[list[str]] = None,
                      tran_sweep: Optional[dict] = None,
                      tran_output_nodes: Optional[list[str]] = None,
                      loop_break: Optional[dict] = None
                      ) -> tuple[str, list[str], set[str]]:
    """Return (netlist_text, commands, spice_nodes_used).

    `netlist_text` is everything from element definitions through stabilizer
    resistors (ready to be loaded into ngspice via a file or `load_circuit`).
    `commands` is the list of control-block commands (`tran ...`, `ac ...`,
    `print v(...)`, etc.) — subprocess-mode execution wraps them in
    `.control`/`.endc` via `deck_to_text()`; PySpice mode feeds them one
    at a time to `exec_command()`.

    Exactly one of {None, ac_sweep, tran_sweep} may be non-None:
      - None       → .op (DC operating point)
      - ac_sweep   → .ac sweep with AC=1 injected at source_node
      - tran_sweep → .tran with optional PWL step_source replacing a rail

    `loop_break` — Layer-4b Middlebrook voltage injection. When set, requires
    ac_sweep to be present too; the AC source is the loop-break V (rail-AC
    and V_ac_only injection are skipped). Keys:
      - tag: unique suffix for element naming
      - original_node: the pre-existing SPICE node the loop breaks off
      - injected_node: the new SPICE node the broken pin was redirected to
        (caller mutates nl.pin_net to make this happen)
    Emits `V_loop_inj_<tag> <original_node> <injected_node> AC 1 DC 0`.
    """
    assert not (ac_sweep and tran_sweep), "ac_sweep and tran_sweep are mutually exclusive"
    assert not (loop_break and not ac_sweep), "loop_break requires ac_sweep"
    lines: list[str] = [
        f"* auto-generated by sim_harness/dc_op_check.py",
        f"* scenario: {scenario['name']}",
        "",
    ]
    used_nodes: set[str] = set()
    rail_class_map = scenario.get("rail_class_map", {})
    # When loop_break is active, the injection V source IS the AC stimulus;
    # don't tag any rail with AC=1.
    ac_source_node = (spice_node(ac_sweep["source_node"])
                       if (ac_sweep and not loop_break) else None)

    # tran step_source overrides an existing rail's voltage source with a PWL
    tran_step = tran_sweep.get("step_source") if tran_sweep else None
    tran_step_node = spice_node(tran_step["node"]) if tran_step else None

    # 1) power rails: ideal voltage sources (add AC=1 on the sweep source,
    # or skip if the node is overridden by a tran step_source)
    for rail, voltage in scenario["power_rails"].items():
        sn = spice_node(rail)
        used_nodes.add(sn)
        if sn == tran_step_node:
            # Emit as PWL step instead of DC rail
            t0 = max(0.0, tran_step["at_time_s"] - tran_step.get("rise_time_s", 1e-9) / 2)
            t1 = tran_step["at_time_s"] + tran_step.get("rise_time_s", 1e-9) / 2
            lines.append(f"V_rail_{spice_ref(rail)} {sn} 0 "
                          f"PWL(0 {tran_step['before_v']:g} "
                          f"{t0:g} {tran_step['before_v']:g} "
                          f"{t1:g} {tran_step['after_v']:g})")
            continue
        ac_tag = " AC 1" if sn == ac_source_node else ""
        lines.append(f"V_rail_{spice_ref(rail)} {sn} 0 DC {voltage}{ac_tag}")

    # 1b) AC source at a node not declared as a rail → dc=0, ac=1
    # Skipped when loop_break is active (V_loop_inj provides the AC stimulus).
    if ac_sweep is not None and loop_break is None:
        rail_nodes = {spice_node(r) for r in scenario["power_rails"]}
        if ac_source_node not in rail_nodes:
            used_nodes.add(ac_source_node)
            lines.append(f"V_ac_only_{spice_ref(ac_sweep['source_node'])} "
                          f"{ac_source_node} 0 DC 0 AC 1")

    # 1c) tran step_source at a node not declared as a rail → emit as PWL only
    if tran_step is not None:
        rail_nodes = {spice_node(r) for r in scenario["power_rails"]}
        if tran_step_node not in rail_nodes:
            used_nodes.add(tran_step_node)
            t0 = max(0.0, tran_step["at_time_s"] - tran_step.get("rise_time_s", 1e-9) / 2)
            t1 = tran_step["at_time_s"] + tran_step.get("rise_time_s", 1e-9) / 2
            lines.append(f"V_step_{spice_ref(tran_step['node'])} "
                          f"{tran_step_node} 0 "
                          f"PWL(0 {tran_step['before_v']:g} "
                          f"{t0:g} {tran_step['before_v']:g} "
                          f"{t1:g} {tran_step['after_v']:g})")

    # 2) walk every component, emit appropriate SPICE element(s)
    device_kinds_used: set[str] = set()
    subckt_defs: list[str] = []       # .subckt blocks from ac_model
    behavioral_x_lines: list[str] = []  # X-instances for ac_model / subckt
    include_paths: set[str] = set()   # deduped .include targets
    xspice_model_lines: list[str] = []  # .model lines for xspice_model
    xspice_a_lines: list[str] = []      # A-instances for xspice_model
    needs_uic = False   # set if any transient_model was emitted — forces
                        # `tran ... uic` so clamp-based sources don't get
                        # collapsed to their steady state by DC op-point
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
            spec_path, spec = spec_hit
            # Precedence: xspice_model > behavioral_spice_subckt > ac_model
            # > per-pin dc_model. transient_model is orthogonal — active
            # only in .tran runs; suppresses the pin's dc_model but not
            # its ac_model participation.
            owned: set[str] = set()
            xspice = spec.get("xspice_model")
            subckt_ref = spec.get("behavioral_spice_subckt")
            ac_model = spec.get("ac_model")
            if xspice is not None:
                cov = _emit_xspice_model(xspice_a_lines, xspice_model_lines,
                                          comp, spec, xspice, nl, used_nodes)
                if cov:
                    owned = cov
            elif subckt_ref is not None:
                cov = _emit_subckt_instance(include_paths, behavioral_x_lines,
                                             comp, spec, spec_path, subckt_ref,
                                             nl, used_nodes)
                if cov:
                    owned = cov
            elif ac_model is not None:
                cov = _emit_ac_model(subckt_defs, behavioral_x_lines, comp,
                                      spec, ac_model, nl, used_nodes)
                if cov:
                    owned = cov
            if tran_sweep is not None:
                tran_covered = _emit_ic_transient_models(lines, comp, spec, nl,
                                                          used_nodes)
                if tran_covered:
                    needs_uic = True
                owned = owned | tran_covered
            _emit_ic_dc_models(lines, comp, spec, nl, rail_class_map,
                                used_nodes, skip_pin_indices=owned)

    # 2b) .model directives for whichever device kinds we emitted
    lines.append("")
    for dk in sorted(device_kinds_used):
        lines.append(_MODEL_LIBRARY[dk])

    # 2c) .include lines for vendor behavioral subckts (deduped)
    for path in sorted(include_paths):
        lines.append(f".include {path}")

    # 2d) auto-generated .subckt blocks from ac_model
    for line in subckt_defs:
        lines.append(line)

    # 2e) X-instances for both ac_model and behavioral_spice_subckt
    for line in behavioral_x_lines:
        lines.append(line)

    # 2e-2) XSPICE .model + A-instance lines. The .cm files themselves
    # must be loaded via `codemodel` before netlist parse — see
    # collect_xspice_cm_paths() and the backend layer.
    for line in xspice_model_lines:
        lines.append(line)
    for line in xspice_a_lines:
        lines.append(line)

    # 2f) Layer-4b Middlebrook voltage-injection source
    if loop_break is not None:
        used_nodes.add(loop_break["original_node"])
        used_nodes.add(loop_break["injected_node"])
        lines.append(f"V_loop_inj_{loop_break['tag']} "
                      f"{loop_break['original_node']} "
                      f"{loop_break['injected_node']} AC 1 DC 0")

    # 3) 1 GΩ stabilizer on every non-GND node. Ngspice is case-insensitive
    # for node names, so dedupe case-variants before emitting to avoid
    # "device already exists" errors when two emitters produced the same
    # node in different cases (e.g. loop-break lower-cased vs X-line raw).
    for node in sorted({n.lower() for n in used_nodes if n != "0"}):
        lines.append(f"R_stab_{node} {node} 0 {STABILIZER_R_OHMS}")

    # 4) control commands — kept as a separate list so backends can consume
    # them differently. Subprocess backend wraps them in .control/.endc;
    # PySpice backend feeds them one at a time to exec_command().
    # NB: ngspice truncates long node names in fixed-width print column
    # headers (`set width` doesn't affect per-column width). Parsers
    # compensate via prefix-match against expected_nodes — see
    # parse_ac_output / parse_tran_output.
    commands: list[str] = []
    if ac_sweep is not None:
        sw = ac_sweep["sweep"]
        kind = {"decade": "dec", "octave": "oct", "linear": "lin"}[sw["kind"]]
        n = sw.get("n_points", 100) if sw["kind"] == "linear" else sw.get("points_per_decade", 10)
        commands.append(f"ac {kind} {n} {sw['start_hz']} {sw['stop_hz']}")
        for out_node in (ac_output_nodes or []):
            commands.append(f"print v({out_node})")
    elif tran_sweep is not None:
        sw = tran_sweep["sweep"]
        # ngspice tran syntax: `tran <tstep> <tstop> [tstart] [uic]`.
        # `uic` skips the DC op-point and starts from initial conditions;
        # required when any transient_model with clamp behavior is present
        # (its DC steady state is post-ramp, not pre-ramp — without uic
        # ngspice would report V(pin)=clamp_v from t=0).
        uic_tag = " uic" if needs_uic else ""
        if sw.get("start_s", 0) > 0:
            commands.append(f"tran {sw['step_s']:g} {sw['stop_s']:g} {sw['start_s']:g}{uic_tag}")
        else:
            commands.append(f"tran {sw['step_s']:g} {sw['stop_s']:g}{uic_tag}")
        for out_node in (tran_output_nodes or []):
            commands.append(f"print v({out_node})")
    else:
        commands.extend(["op", "let out = 0", "print all"])
    lines.append("")  # blank line before .end
    return "\n".join(lines) + "\n", commands, used_nodes


def collect_xspice_cm_paths(nl: Netlist, specs: dict) -> list[str]:
    """Union of all `.cm` file paths referenced by any matched IC's
    `xspice_model.cm_path`. Relative paths resolve against the spec's
    directory; absolute paths pass through. Deduped, sorted for
    determinism. Returned as strings (paths ready for `codemodel` command)."""
    paths: set[str] = set()
    for comp in nl.comps.values():
        hit = match_spec(comp, specs)
        if hit is None:
            continue
        spec_path, spec = hit
        xm = spec.get("xspice_model")
        if xm is None:
            continue
        raw = xm["cm_path"]
        p = Path(raw)
        if not p.is_absolute():
            p = spec_path.parent / raw
        paths.add(str(p.resolve()))
    return sorted(paths)


def deck_to_text(netlist_text: str, commands: list[str]) -> str:
    """Combine (netlist, commands) into a single ngspice deck for
    subprocess-mode execution. Wraps commands in .control/.endc,
    appends .end."""
    ctl = "\n".join([".control", *commands, ".endc", ".end"])
    # netlist_text always ends with a newline
    return netlist_text + ctl + "\n"


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


def parse_ac_output(text: str, expected_nodes: Optional[list[str]] = None
                    ) -> dict[str, list[tuple[float, complex]]]:
    """Extract per-node frequency/complex-voltage samples from ngspice AC output.
    Returns {output_node: [(freq_hz, complex_value), ...]}.

    Each `print v(node)` produces one column-block preceded by a header line
    naming the node. Ngspice truncates long node names in that header (fixed-
    width columns; `set width=N` doesn't help). If `expected_nodes` is given
    (the list the runner asked for, in emission order), header lines are
    matched positionally against that list — robust to any header truncation."""
    blocks: dict[str, list[tuple[float, complex]]] = {}
    current_node: Optional[str] = None
    header_re = re.compile(r"^\s*Index\s+frequency\s+v\((?P<name>[^)\s]*)",
                           re.IGNORECASE)
    for line in text.splitlines():
        h = header_re.match(line)
        if h:
            current_node = _resolve_truncated_node(h.group("name"), expected_nodes)
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


def _resolve_truncated_node(header_name: str,
                            expected_nodes: Optional[list[str]]) -> Optional[str]:
    """Ngspice truncates long node names in fixed-width column headers.
    Given the truncated prefix from the header (e.g. `n_buckboost_b`), find
    the expected node whose sanitized SPICE name starts with that prefix.
    Falls back to the truncated prefix if no expected list is given or no
    unique match exists (in which case downstream sample lookups may miss)."""
    if not header_name:
        return None
    name_lc = header_name.lower()
    if expected_nodes:
        matches = [n.lower() for n in expected_nodes
                   if n.lower().startswith(name_lc)]
        if len(matches) == 1:
            return matches[0]
    # Fall back to the (possibly truncated) name from the header
    return name_lc


def output_nodes_for_sweep(sweep: dict) -> list[str]:
    """Union of the sweep-level output_node and any per-expected-point overrides.
    Used for both ac_sweeps and tran_sweeps (same structural shape)."""
    default = sweep.get("output_node")
    per_point = [p.get("output_node") for p in sweep.get("expected", [])]
    all_nodes = {default} | set(per_point)
    all_nodes.discard(None)
    return sorted(all_nodes)


# ngspice .tran output row: `<idx>\t<time>\t<value>\t`
_TRAN_LINE_RE = re.compile(
    r"^\s*\d+\s+(-?\d+\.\d+e[+-]?\d+)\s+"
    r"(-?\d+\.\d+e[+-]?\d+)\s*$"
)


def parse_tran_output(text: str, expected_nodes: Optional[list[str]] = None
                      ) -> dict[str, list[tuple[float, float]]]:
    """Extract per-node time/voltage samples from ngspice .tran output.
    Returns {output_node: [(time_s, voltage_v), ...]}.

    See parse_ac_output for the truncated-header explanation. Same fix:
    pass expected_nodes to match header lines positionally by emission order.
    The 'Initial Transient Solution' section at the top of the ngspice log
    (different format) is naturally ignored — its rows don't match the
    `<idx>\\t<time>\\t<val>` pattern."""
    blocks: dict[str, list[tuple[float, float]]] = {}
    current_node: Optional[str] = None
    header_re = re.compile(r"^\s*Index\s+time\s+v\((?P<name>[^)\s]*)",
                           re.IGNORECASE)
    for line in text.splitlines():
        h = header_re.match(line)
        if h:
            current_node = _resolve_truncated_node(h.group("name"), expected_nodes)
            if current_node is not None:
                blocks.setdefault(current_node, [])
            continue
        if current_node is None:
            continue
        m = _TRAN_LINE_RE.match(line)
        if not m:
            continue
        t = float(m.group(1))
        v = float(m.group(2))
        blocks[current_node].append((t, v))
    return blocks
