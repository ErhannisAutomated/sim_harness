# sim_harness — PCB design-verification harness

Layered checks that validate a schematic against machine-readable
component specs.  Layer 1 (this file) does static per-pin rule checks;
Layers 2–5 (topology comparison, DC / AC / transient simulation) are
planned but not yet built.

## Motivation

An LLM without a verification loop produces plausible-but-wrong
designs — we discovered three wiring bugs on the LM5176 buck-boost
subcircuit of `power_module_v2alt` that had been introduced by a
prior session hallucinating pin functions.  This harness is the
verification loop.

See `[[project-sim-harness]]` in auto-memory for the full plan.

## Layout

```
sim_harness/
  static_check.py               # Layer-1 checker CLI
  test_static_check.py          # regression test (LM5176 bugs)
  schema/
    component_spec.v0.schema.json
components/                     # top-level, sibling to sim_harness/
  LM5176/
    v0.json                     # pin constraints
    _source.meta.json           # datasheet provenance + extraction log
```

## Running

Layer 1, against a schematic in the repo:

```bash
python sim_harness/static_check.py \
  projects/power_module_v2alt/power_module_v2alt.kicad_sch
```

The checker runs `kicad-cli sch export netlist --format kicadxml`
under the hood.  Pre-exported netlist:

```bash
kicad-cli sch export netlist --format kicadxml \
  -o v2alt.net.xml projects/power_module_v2alt/power_module_v2alt.kicad_sch
python sim_harness/static_check.py --netlist v2alt.net.xml \
  projects/power_module_v2alt/power_module_v2alt.kicad_sch
```

Exit code is 0 iff no error-level violations.

## Adding a component spec

1. Create `components/<MPN>/v0.json` matching
   `sim_harness/schema/component_spec.v0.schema.json`.
2. Set `mpn` to the base part number the schematic will use (or a
   prefix — the checker does longest-prefix match against symbol MPN /
   Value).
3. Declare `net_families` for named net classes referenced by
   constraints (AGND, VCC, SWITCHING_NODE, …).
4. For each pin whose datasheet imposes a hard rule, list constraints:
   - `must_connect_through` — a series component of a given type
     (and optional value range) between the pin and a target family.
   - `forbidden_direct_connection` — the pin's net must not match a
     given family.
   - `paired_with` — informational at v0; the partner-pin bookkeeping.
5. Validate: `python -c 'import json,jsonschema; jsonschema.Draft202012Validator(json.load(open("sim_harness/schema/component_spec.v0.schema.json"))).validate(json.load(open("components/<MPN>/v0.json")))'`.

## Current coverage

Only `LM5176` (5 of 28 pins — enough to catch the three known
`power_module_v2alt` bugs).  Full LM5176 extraction and the other
active ICs (BQ76920, IP2326, CH224K, AON7544) are the next work
items — planned via the multi-agent extraction pipeline described in
the sim-harness project memo.
