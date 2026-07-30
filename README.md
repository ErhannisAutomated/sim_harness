# sim_harness — PCB design-verification harness

Layered checks that validate a schematic against machine-readable
component specs. As of 2026-07-30, five layers shipped:

- **Layer 1** — static per-pin topology rules (`static_check.py`)
- **Layer 3a** — DC operating-point solve of the linear network (`dc_op_check.py`)
- **Layer 3b** — per-pin `dc_check` assertions from component specs
- **Layer 3c** — semiconductor DC via ngspice built-in device models (diodes, MOSFETs)
- **Layer 4a** — small-signal AC sweep with magnitude/dB expectations
- **Layer 5a** — transient sim with PWL step stimulus and time-point expectations

Deferred: Layer 2 (topology comparator against vendor reference designs),
Layer 4b (small-signal averaged models for regulator loops), Layer 5b/c/d
(PWM stimulus, XSPICE code models in Rust, libngspice FFI).

## Motivation

An LLM without a verification loop produces plausible-but-wrong
designs — we discovered three wiring bugs on the LM5176 buck-boost
subcircuit of `power_module_v2alt` that had been introduced by a
prior session hallucinating pin functions.  This harness is the
verification loop. As of 2026-07-30 the shipped layers have found
**8 real bugs** on that project.

See `[[project-sim-harness]]` in auto-memory for the full plan.

## Layout

```
sim_harness/
  static_check.py               # Layer-1 checker CLI
  dc_op_check.py                # Layer 3/4/5 checker CLI (thin — orchestration)
  spice.py                      # SPICE deck generation, ngspice invocation, raw parsers
  checks.py                     # DC + AC + tran evaluators, Result dataclasses
  test_static_check.py          # Layer-1 regression tests (real bugs + negative controlled)
  test_dc_op_check.py           # Layer 3/4/5 tests (RC/Norton/diode/MOSFET/AC/tran + negatives)
  schema/
    component_spec.v0.schema.json
    scenario.v0.schema.json
  tests/fixtures/               # synthetic netlists + scenarios for regression tests
components/                     # top-level, sibling to sim_harness/
  LM5176/
    v1.json                     # pin constraints (+ dc_model / dc_checks per pin)
    _source.meta.json           # datasheet provenance + extraction log
```

## Running

**Layer 1** (static topology) against a schematic in the repo:

```bash
python sim_harness/static_check.py \
  projects/power_module_v2alt/power_module_v2alt.kicad_sch
```

**Layer 3/4/5** requires a scenario JSON declaring power rails + optional
`expected` values / `ac_sweeps` / `tran_sweeps`:

```bash
python sim_harness/dc_op_check.py \
  projects/power_module_v2alt/scenarios/full_pack_20v_usb.json \
  projects/power_module_v2alt/power_module_v2alt.kicad_sch
```

Both checkers accept `--netlist <pre-exported-xml>` to skip the
`kicad-cli sch export netlist --format kicadxml` step. Exit code 0 iff
every check passed.

**Sandbox caveat:** ngspice's glibc `tmpfile()` writes to `/tmp/tmpXXXXXX`,
which the sandbox typically blocks. Bash calls that shell out to ngspice
need `dangerouslyDisableSandbox: true`, or `/tmp/tmp*` needs to be in the
`sandbox.filesystem.allowWrite` allowlist. See `[[feedback-ngspice-sandbox]]`.

## Adding a component spec

1. Create `components/<MPN>/v1.json` matching
   `sim_harness/schema/component_spec.v0.schema.json`.
2. Set `mpn` to the base part number the schematic will use (or a
   prefix — the checker does longest-prefix match against symbol MPN /
   Value).
3. Declare `net_families` for named net classes referenced by
   constraints (AGND, VCC, SWITCHING_NODE, …).
4. For each pin, capture as applicable:
   - **`voltage_range`** (abs-max range from the datasheet).
   - **`constraints`** — Layer-1 topology assertions:
     - `must_connect_through` — a series component of a given type
       (and optional value range) between the pin and a target family.
     - `forbidden_direct_connection` — the pin's net must not match a
       given family.
     - `paired_with` — informational; partner-pin bookkeeping.
   - **`dc_model`** — Layer-3 behavior: `high_z` (default), `driven` with
     a `voltage_v` or `follows_rail` (Thevenin), or `sourced` with a
     `current_a` + `direction` (Norton).
   - **`dc_checks`** — Layer-3b assertions on the pin's solved DC voltage:
     `must_equal`, `must_exceed`, `must_be_below`, `must_be_in_range`.
5. Validate: `python -c 'import json,jsonschema; jsonschema.Draft202012Validator(json.load(open("sim_harness/schema/component_spec.v0.schema.json"))).validate(json.load(open("components/<MPN>/v1.json")))'`.

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
