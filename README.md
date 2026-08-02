# sim_harness — PCB design-verification harness

Layered checks that validate a schematic against machine-readable
component specs. As of 2026-07-31, eight layers shipped:

- **Layer 1** — static per-pin topology rules (`static_check.py`)
- **Layer 3a** — DC operating-point solve of the linear network (`dc_op_check.py`)
- **Layer 3b** — per-pin `dc_check` assertions from component specs
- **Layer 3c** — semiconductor DC via ngspice built-in device models (diodes, MOSFETs)
- **Layer 4a** — small-signal AC sweep with magnitude/dB expectations
- **Layer 4b** — closed-loop stability via Middlebrook voltage injection
  (crossover frequency, phase margin, gain margin)
- **Layer 5a** — transient sim with PWL step stimulus and time-point expectations
- **Layer 5c** — per-pin `transient_model` topologies for state-machine-flavored
  IC behavior (soft-start ramps, boot delays, gated outputs)

Behavioral IC modeling framework (shipped 2026-07-30/31):
- Spec-level `ac_model` — parametric linear IC model (currently
  `op_amp_single_pole`; A0 / GBW / output_z → auto-generated single-pole
  `.subckt`). Used by both DC op-point (Layer 3a) and AC sweep (Layer 4a).
- Spec-level `behavioral_spice_subckt` — reference to an external vendor
  `.subckt` file. `.include`d verbatim; terminals wired in declaration order.
- Spec-level `xspice_model` — reference to an XSPICE `.cm` code-model
  (compiled shared library). Runner auto-loads the `.cm` before netlist
  parse, emits `.model` + `A<ref>` device. Supports `differential`
  (`%vd(pos neg)`) and `single`-ended connections. Precedence: overrides
  `behavioral_spice_subckt`, `ac_model`, and per-pin models.

Deferred: Layer 2 (topology comparator against vendor reference designs),
Layer 5b/c/d (PWM stimulus, XSPICE code models in Rust, libngspice FFI).

## Layer 5c: transient_model topologies

Per-pin `transient_model` at spec level captures time-domain behavior the
datasheet promises (SS ramp, POR delay, PGOOD gating). Three topologies:

- **`current_source_with_clamp`** — sources fixed current into pin until
  V(pin) reaches clamp_v, then holds. Emitted as B-source with `u()`
  clamp. Runner adds `uic` to the `.tran` directive when any
  transient_model is present, so caps start at 0V rather than at their
  post-ramp DC steady state.
- **`voltage_after_delay`** — pin holds at `before_v` until time
  `delay_s`, then transitions to `after_v`. Emitted as PWL.
- **`voltage_gated_by_input`** — pin follows `high_v`/`low_v` based on
  whether V(other_pin) exceeds `threshold_v`. Optional `stable_for_s`
  adds a first-order filter (G+R+C) on the sense side to model
  "input must be stable for N µs" delay.

`transient_model` is orthogonal to `dc_model` / `ac_model`: emitted only
during `.tran` runs, suppresses the pin's `dc_model` for that run only.

## Modeling metadata

Spec-level `modeling.capabilities` (positive assertions of what the spec
verifies) and `modeling.simplifications` (documented deviations from
datasheet fidelity). Scenarios can declare `requires_capabilities: [...]`;
runner does a pre-flight check and warns (not fails) on gaps. Bare-string
tags default to `{tag, disposition: "claimed"}`; promote to
`{tag, disposition: "verified" | "failing", note}` when appropriate.

`provenance` block (optional) records extractor runs, datasheet SHA-256,
and per-field consensus from multi-agent extraction. NOT a confidence
score — records what was done, not what it means.

## Layer 4b: loop-stability convention

`loop_stability.<name>.break_at` names a component pin — but **name the
FEEDBACK SENSE pin (op-amp IN-, regulator FB), NOT the driver output**.
Breaking at the driver disconnects its load impedance from the driver,
which erases the very effects (cap-load poles, resonance) we're trying
to measure. Breaking at the sense pin keeps the driver + all its loads
on the original net; only the ONE high-impedance sense pin moves to the
fresh injection-side net. See the docstring on `_run_loop_stability`
and the fixture at `tests/fixtures/op_amp_buffer_*`.

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

**Backend selection** (Layer-3+ checker only). Two backends implement
the simulation-runner interface; both produce numerically-equivalent
results (locked in by `test_backends.py`):

- `--backend subprocess` (default) — spawns `ngspice -b <deck.cir>` per
  run, parses stdout. Portable, works anywhere ngspice is installed.
- `--backend pyspice` — embeds libngspice in-process via PySpice's
  NgSpiceShared. Structured data (no stdout regex), foundation for
  Stages 5d-2 (XSPICE .cm loading) and 5d-3 (callback-driven adaptive
  stimulus). Requires `libngspice0` and `pip install PySpice`. If
  libngspice isn't at `/usr/lib/x86_64-linux-gnu/libngspice.so.0`,
  override via `SIM_HARNESS_LIBNGSPICE=/path/to/libngspice.so`. Perf
  win only shows in long-lived processes (test harness with per-test
  Python startup doesn't benefit from FFI vs. subprocess yet).

**Sandbox caveat:** ngspice's glibc `tmpfile()` writes to `/tmp/tmpXXXXXX`,
which the sandbox typically blocks. Same issue applies to both backends
(FFI is in-process but the C `tmpfile()` call still hits `/tmp`). Bash
calls need `dangerouslyDisableSandbox: true`, or `/tmp/tmp*` in the
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
5. **Optional, IC-wide behavioral model.** For op-amps and similar linear
   ICs, add a spec-level **`ac_model`** naming the topology + parameters
   (`op_amp_single_pole` today; A0 in dB, GBW in Hz, output_z in Ω, plus
   the (in_plus, in_minus, output) pin_index refs). The runner emits a
   canonical `.subckt` per matched IC and instantiates it — you get DC
   op-point + AC sweep behavior for free. For vendor SPICE models,
   spec-level **`behavioral_spice_subckt`** points to a `.sub`/`.lib` file
   with an ordered list of `(subckt_terminal, pin_index)` pairs; the file
   is `.include`d and terminals wired positionally. Both suppress per-pin
   `dc_model` emission on the covered pins.
6. Validate: `python -c 'import json,jsonschema; jsonschema.Draft202012Validator(json.load(open("sim_harness/schema/component_spec.v0.schema.json"))).validate(json.load(open("components/<MPN>/v1.json")))'`.

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
