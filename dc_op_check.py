#!/usr/bin/env python3
"""Layer-3 (DC operating-point) and Layer-4a (small-signal AC sweep) checker.

Given a KiCad schematic + a scenario JSON (declared power rails, expected
node voltages, and/or AC sweeps) + the component-spec directory, this:

  1. Builds a SPICE deck from the linear network (see sim_harness/spice.py).
  2. Runs ngspice's .op analysis; parses node voltages.
  3. Runs one .ac analysis per scenario `ac_sweeps` entry.
  4. Evaluates scenario `expected` values, per-pin `dc_checks` from the
     component specs, and per-sweep `expected` (magnitude in dB).
  5. Reports pass/fail. Exit code 0 iff every check passed.

Splits: spice.py (deck generation + ngspice invocation + raw parsers);
checks.py (evaluators + result classes); static_check.py (netlist +
spec loading, plus Layer 1 topology checker).
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Make sibling modules importable without installing the package.
sys.path.insert(0, str(Path(__file__).parent))

from static_check import load_netlist, load_specs                       # noqa: E402
from spice import (                                                     # noqa: E402
    build_spice_deck, run_ngspice, parse_node_voltages, parse_ac_output,
    spice_node, output_nodes_for_sweep,
)
from checks import (                                                    # noqa: E402
    AcResult, Result, CheckResult,
    compare_expected, evaluate_dc_checks, evaluate_ac_sweep,
    format_results,
)


SCENARIO_SCHEMA_PATH = Path(__file__).parent / "schema" / "scenario.v0.schema.json"


def load_scenario(path: Path) -> dict:
    import jsonschema
    schema = json.loads(SCENARIO_SCHEMA_PATH.read_text())
    scenario = json.loads(path.read_text())
    jsonschema.Draft202012Validator(schema).validate(scenario)
    return scenario


def _print_dc_check_results(check_results: list[CheckResult]):
    print("--- component spec `dc_checks` ---")
    for r in check_results:
        tag = {"pass": "PASS   ", "fail": "FAIL   ", "missing": "MISSING"}[r.status]
        actual = f"{r.actual_v:+.4f} V" if r.actual_v is not None else "(no voltage)"
        print(f"[{tag}] {r.ic_ref} pin {r.pin} ({r.pin_name}): "
              f"{actual} — {r.description}")
        if r.status == "fail" and r.rationale:
            print(f"          rationale: {r.rationale}")


def _print_ac_results(ac_results: list[AcResult]):
    print("--- scenario `ac_sweeps` ---")
    for r in ac_results:
        tag = {"pass": "PASS   ", "fail": "FAIL   ", "missing": "MISSING"}[r.status]
        actual = f"{r.actual_db:+.2f} dB" if r.actual_db is not None else "(no data)"
        print(f"[{tag}] {r.sweep_name} @ {r.freq_hz:g} Hz → {r.output_node}: "
              f"{actual} (expected {r.expected_db:+.2f} ±{r.tolerance_db})")
        if r.status == "fail" and r.rationale:
            print(f"          rationale: {r.rationale}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scenario", type=Path, help="path to scenario JSON")
    ap.add_argument("schematic", type=Path, nargs="?",
                    help="path to schematic (falls back to --netlist if omitted)")
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

    # DC op-point pass
    deck_text, _ = build_spice_deck(nl, specs, scenario)
    deck_path = args.work_dir / (scenario["name"] + ".cir")
    deck_path.write_text(deck_text)
    dc_output = run_ngspice(deck_path, args.work_dir)
    voltages = parse_node_voltages(dc_output)
    results = compare_expected(voltages, scenario)
    check_results = evaluate_dc_checks(nl, specs, voltages)

    # AC sweeps — one ngspice run per sweep
    ac_results: list[AcResult] = []
    for sweep_name, sweep in scenario.get("ac_sweeps", {}).items():
        out_nodes = [spice_node(n).lower() for n in output_nodes_for_sweep(sweep)]
        ac_deck_text, _ = build_spice_deck(nl, specs, scenario,
                                            ac_sweep=sweep,
                                            ac_output_nodes=out_nodes)
        ac_deck_path = args.work_dir / f"{scenario['name']}_ac_{sweep_name}.cir"
        ac_deck_path.write_text(ac_deck_text)
        ac_output = run_ngspice(ac_deck_path, args.work_dir)
        samples = parse_ac_output(ac_output)
        ac_results.extend(evaluate_ac_sweep(sweep_name, sweep, samples))

    # Report
    if results:
        print("--- scenario `expected` checks ---")
        print(format_results(results))
    if check_results:
        _print_dc_check_results(check_results)
    if ac_results:
        _print_ac_results(ac_results)

    all_status = ([r.status for r in results]
                  + [r.status for r in check_results]
                  + [r.status for r in ac_results])
    n_pass = sum(1 for s in all_status if s == "pass")
    n_fail = sum(1 for s in all_status if s == "fail")
    n_miss = sum(1 for s in all_status if s == "missing")
    print(f"\n{n_pass} pass, {n_fail} fail, {n_miss} missing "
          f"(of {len(all_status)} checks)")

    if not args.keep_deck and all(s == "pass" for s in all_status):
        for p in [deck_path] + list(args.work_dir.glob(f"{scenario['name']}_ac_*.cir")):
            if p.exists():
                p.unlink()

    return 0 if all(s == "pass" for s in all_status) else 1


if __name__ == "__main__":
    sys.exit(main())
