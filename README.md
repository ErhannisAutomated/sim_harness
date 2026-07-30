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

- **LM5176** — 29 pins, all constrained (multi-agent extraction).
- **BQ76920** — 20 pins (TSSOP-20 variant).
- **IP2326** — 25 pins.
- **CH224K** — 11 pins.
- **AON7544** — 9 pads with G/D/S aliases on datasheet pad numbers.

## Adding a new IC via the multi-agent extraction workflow

The `Workflow` tool runs a fresh N-agent extractor panel + judge + adversarial
verifier per IC.  The v0-schema-compliant JSON lands under
`components/<MPN>/v1.json` after light post-processing.  Two prompt
hardenings worth carrying forward on any new extraction workflow, based
on pitfalls we hit in the LM5176 and 4-IC runs:

1. **Judge output MUST NOT contain JSON-invalid ellipsis.** A judge asked
   to summarise a long `candidate_values` array sometimes writes
   `[1,2,3,...]` inside a JSON block.  This breaks `JSON.parse`.
   In the judge prompt, add:
   > CRITICAL: your output must be valid JSON. Do NOT use `...` as an
   > ellipsis inside arrays or objects. If a `candidate_values` array
   > would be inconveniently long, replace it with the first few entries
   > plus a `truncated_count` sibling field.
2. **Pin index / footprint pad mismatch is normal for MOSFETs and QFN
   parts.** Datasheets number pads 1..N (plus "EP"); KiCad symbols
   often use functional names ("G", "D", "S") or renumber EP to `N+1`.
   The v0 schema supports `"index": [1, "S"]`-style alias lists — tell
   the judge to add the schematic-native alias where obvious, otherwise
   note the mismatch in the pin's `notes` for a post-hoc alias patch.

CH224K's judge run in the 2026-07-30 batch hit pitfall (1); LM5176's
EP and AON7544's G/D/S both hit pitfall (2).
