#!/usr/bin/env python3
"""Layer-1 static checker.

Reads component-spec JSONs from a components directory and validates them
against a KiCad schematic netlist (obtained via `kicad-cli sch export
netlist --format kicadxml`).  Emits per-pin rule violations.  Exit code
0 = no errors, 1 = at least one error-level violation.
"""

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SI_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
             "m": 1e-3, "k": 1e3, "meg": 1e6, "M": 1e6, "G": 1e9}
_VAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(meg|p|n|u|µ|m|k|M|G)?\s*[a-zA-Zµ]*\s*$")


def parse_value(s: str) -> Optional[float]:
    """'20k' -> 20000.0, '10nF' -> 1e-8, '6.8uH' -> 6.8e-6, '15m' -> 0.015."""
    m = _VAL_RE.match(s or "")
    if not m:
        return None
    return float(m.group(1)) * SI_PREFIX.get(m.group(2) or "", 1.0)


@dataclass
class Comp:
    ref: str
    value: str
    libsource_part: str
    mpn: Optional[str] = None

    @property
    def part_type(self) -> str:
        p = (self.libsource_part or "").strip()
        if p == "R" or p.startswith("R_"): return "resistor"
        if p == "C" or p.startswith("C_"): return "capacitor"
        if p == "L" or p.startswith("L_"): return "inductor"
        if p == "D" or p.startswith("D_"): return "diode"
        return "ic"


@dataclass
class Netlist:
    comps: dict[str, Comp]
    nets: dict[str, list[tuple[str, str]]]  # net name -> [(ref, pin), ...]
    pin_net: dict[tuple[str, str], str] = field(default_factory=dict)


def load_netlist(xml_path: Path) -> Netlist:
    root = ET.parse(xml_path).getroot()
    comps: dict[str, Comp] = {}
    components_el = root.find("components")
    for c in (components_el if components_el is not None else []):
        ref = c.get("ref") or ""
        value = (c.findtext("value") or "").strip()
        libsource = c.find("libsource")
        libpart = libsource.get("part") if libsource is not None else ""
        mpn = None
        for p in c.iter("property"):
            if (p.get("name") or "").lower() == "mpn":
                mpn = p.get("value")
                break
        comps[ref] = Comp(ref, value, libpart or "", mpn)
    nets: dict[str, list[tuple[str, str]]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    nets_el = root.find("nets")
    for net in (nets_el if nets_el is not None else []):
        name = net.get("name") or ""
        nodes = [(nd.get("ref") or "", nd.get("pin") or "") for nd in net.iter("node")]
        nets[name] = nodes
        for pn in nodes:
            pin_net[pn] = name
    return Netlist(comps=comps, nets=nets, pin_net=pin_net)


SCHEMA_PATH = Path(__file__).parent / "schema" / "component_spec.v0.schema.json"


def load_specs(components_dir: Path) -> dict[str, tuple[Path, dict]]:
    import jsonschema
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    specs: dict[str, tuple[Path, dict]] = {}
    for spec_file in sorted(components_dir.glob("*/v*.json")):
        try:
            spec = json.loads(spec_file.read_text())
        except json.JSONDecodeError as e:
            print(f"warning: {spec_file}: {e}", file=sys.stderr)
            continue
        errors = sorted(validator.iter_errors(spec), key=lambda e: e.path)
        if errors:
            print(f"warning: {spec_file} failed schema validation, skipping:",
                  file=sys.stderr)
            for err in errors:
                print(f"  {'/'.join(str(p) for p in err.path)}: {err.message}",
                      file=sys.stderr)
            continue
        specs[spec["mpn"]] = (spec_file, spec)
    return specs


def match_spec(comp: Comp, specs: dict[str, tuple[Path, dict]]) -> Optional[tuple[Path, dict]]:
    for candidate in (comp.mpn, comp.value):
        if candidate and candidate in specs:
            return specs[candidate]
    best: Optional[tuple[str, tuple[Path, dict]]] = None
    for base, hit in specs.items():
        for candidate in (comp.mpn, comp.value):
            if candidate and candidate.startswith(base):
                if best is None or len(base) > len(best[0]):
                    best = (base, hit)
    return best[1] if best else None


def _local_net(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def net_in_family(name: str, family: dict) -> bool:
    local = _local_net(name)
    return any(re.fullmatch(p, local) for p in family.get("patterns", []))


@dataclass
class Violation:
    severity: str
    ref: str
    pin: str
    rule: str
    message: str
    rationale: str = ""


def check_component(comp: Comp, spec: dict, nl: Netlist) -> list[Violation]:
    families = spec.get("net_families", {})
    violations: list[Violation] = []
    for pin in spec.get("pins", []):
        idx = str(pin["index"])
        name = pin["name"]
        net = nl.pin_net.get((comp.ref, idx))
        if net is None:
            if not pin.get("may_be_unconnected"):
                violations.append(Violation("warning", comp.ref, idx, "unconnected",
                    f"pin {idx} ({name}) has no net in the exported netlist"))
            continue
        for c in pin.get("constraints", []):
            violations.extend(_check_constraint(comp, pin, net, c, families, nl))
    return violations


def _check_constraint(comp: Comp, pin: dict, net: str, c: dict,
                      families: dict, nl: Netlist) -> list[Violation]:
    kind = c["kind"]
    if kind == "forbidden_direct_connection":
        fam = families.get(c["to_net_family"])
        if fam and net_in_family(net, fam):
            return [Violation("error", comp.ref, str(pin["index"]),
                              "forbidden_direct_connection",
                              f"{pin['name']} tied directly to net {net!r} "
                              f"(matches {c['to_net_family']} family)",
                              c.get("rationale", ""))]
        return []
    if kind == "must_connect_through":
        return _check_must_connect_through(comp, pin, net, c, families, nl)
    return []  # paired_with is informational at v0


def _check_must_connect_through(comp: Comp, pin: dict, net: str, c: dict,
                                 families: dict, nl: Netlist) -> list[Violation]:
    fam_name = c["to_net_family"]
    fam = families.get(fam_name)
    if fam is None:
        return []
    want = c["series_component"]
    val_range = c.get("value_range")
    ref_pin = (comp.ref, str(pin["index"]))
    label = pin["name"]

    if net_in_family(net, fam):
        return [Violation("error", comp.ref, ref_pin[1], "must_connect_through",
                          f"{label} tied directly to {fam_name} (net {net!r}); "
                          f"datasheet requires a series {want} between them",
                          c.get("rationale", ""))]

    neighbours = [(r, p) for (r, p) in nl.nets.get(net, []) if (r, p) != ref_pin]
    if not neighbours:
        return [Violation("error", comp.ref, ref_pin[1], "must_connect_through",
                          f"{label} net {net!r} has no other pin on it; "
                          f"datasheet requires a series {want} to {fam_name}",
                          c.get("rationale", ""))]

    wrong_type: list[tuple[Comp, str]] = []
    for (r, p) in neighbours:
        nb = nl.comps.get(r)
        if nb is None:
            continue
        if nb.part_type != want:
            if nb.part_type in ("resistor", "capacitor", "inductor", "diode"):
                wrong_type.append((nb, p))
            continue
        other_pin = "2" if p == "1" else "1"
        other_net = nl.pin_net.get((r, other_pin))
        if other_net and net_in_family(other_net, fam):
            val = parse_value(nb.value)
            if val_range and val is not None:
                lo, hi = val_range
                if not (lo <= val <= hi):
                    return [Violation("error", comp.ref, ref_pin[1], "must_connect_through",
                                      f"{label} series {want} {nb.ref} "
                                      f"value {nb.value!r} (={val:g}) outside required "
                                      f"range [{lo:g}, {hi:g}]",
                                      c.get("rationale", ""))]
            return []

    if wrong_type:
        desc = ", ".join(f"{nb.ref}({nb.part_type} {nb.value})" for nb, _ in wrong_type)
        return [Violation("error", comp.ref, ref_pin[1], "must_connect_through",
                          f"{label} needs a series {want} to {fam_name}, but net "
                          f"{net!r} instead contains: {desc}",
                          c.get("rationale", ""))]
    return [Violation("warning", comp.ref, ref_pin[1], "must_connect_through",
                      f"{label} needs a series {want} to {fam_name}; no path found "
                      f"from net {net!r}",
                      c.get("rationale", ""))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("schematic", type=Path)
    ap.add_argument("--components-dir", type=Path, default=Path("components"))
    ap.add_argument("--netlist", type=Path, help="pre-exported kicadxml netlist; skip kicad-cli")
    ap.add_argument("--work-dir", type=Path, default=Path("/tmp/claude-1000"))
    args = ap.parse_args()

    if args.netlist:
        xml_path = args.netlist
    else:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        xml_path = args.work_dir / (args.schematic.stem + ".net.xml")
        subprocess.run(["kicad-cli", "sch", "export", "netlist",
                        "--format", "kicadxml", "-o", str(xml_path),
                        str(args.schematic)], check=True)

    nl = load_netlist(xml_path)
    specs = load_specs(args.components_dir)
    matched = 0
    violations: list[Violation] = []
    for comp in nl.comps.values():
        spec_hit = match_spec(comp, specs)
        if spec_hit is None:
            continue
        matched += 1
        violations.extend(check_component(comp, spec_hit[1], nl))

    for v in violations:
        print(f"[{v.severity.upper():7}] {v.ref} pin {v.pin} ({v.rule}): {v.message}")
        if v.rationale:
            print(f"          rationale: {v.rationale}")
    print(f"\nchecked {matched} components with specs "
          f"(of {len(nl.comps)} total); {len(violations)} violations")
    return 0 if all(v.severity != "error" for v in violations) else 1


if __name__ == "__main__":
    sys.exit(main())
