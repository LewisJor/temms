# TEMMS progress — updated 2026-10-15

TEMMS remains an alpha Python library for application-owned local inference.
The baseline for this iteration is
[`adf89b4429f72b14d8d9a5aa9744d47b4285228f`](https://github.com/LewisJor/temms/commit/adf89b4429f72b14d8d9a5aa9744d47b4285228f).
The current work strengthens the practical assurance of the real-model demonstration;
it does not change the core or adapters.

## Kept improvement: verify every served output

The old replay repeatedly classified Chelsea and accepted any finite 1,000-score
output. Scene transitions could pass even if a runtime returned scores for the
wrong image. Merely varying the displayed pictures would not close that gap.

**Hypothesis and acceptance:** reusing the existing pinned twenty-image workload
and checking every returned score against direct execution should expose stale
inputs and wrong-model results, including substitutions with the same top-1 class.
Completion requires all seven scenes, checked service of every input and zero output
or admission-identity mismatches. One bad frame must remain a failure even if later
frames look correct. This is one behavior-level change at the example boundary.

`prepare_inputs` computes forty distinct model/input reference vectors before
replay; `check_output` compares all scores with explicit tolerances. The terminal
shows input, expected class, prediction and separate integrity verdicts. JSONL saves
references and actual scores for independent recomputation. Classifier mistakes are
reported separately and do not fail the integration check. The runbook includes the
new preparation step, coverage requirements and interpretation limits.

The native integration suite now forces a request to span publication using distinct
Identity and Negation graphs. The old request must return its original numerical
result and identity after the new model has already served. No production runtime
change was needed. Regression tests also cover wrong inputs/models/digests, malformed
or nonfinite tensors, ambiguous references and permanent failure after one bad frame.

## Current local verification

On macOS ARM64, the current source passed **224 tests, zero skipped**, strict mypy
(**10 files**) and scoped Ruff under Python 3.11.8. The built wheel passed the same
**224 tests without skips** in the existing isolated Python 3.12.15 environment.
Both wheel and source distribution built; a newly created core-only environment
installed the wheel without dependencies and ran the README's tiny example.

The real replay ran with `--frames 12 --interval 0.5`, ONNX Runtime **1.24.1**,
NumPy **2.4.2** and Pillow **12.1.0**. A macOS sandbox denied network operations;
a direct-IP connection failed with `EPERM` before the same sandboxed process started
the replay. Results:

- **84 attempts:** 69 served, three waiting, 12 abstained; all seven scenes passed.
- **69 full-score and admission checks:** zero output or identity mismatches.
- **20/20 inputs** served with verified outputs; **35/40 model/input pairs** exercised.
- **59/69 expected labels correct**, descriptive only: repeated inputs and this small
  dog-only subset do not establish model accuracy or comparative benefit.

Raw records, the network probe and build/install evidence are retained locally in
ignored `.demo-assets/live-swap/integrity-20261015/`, not included in a clone.
The [Ubuntu workflow](../.github/workflows/ubuntu24.yml) independently checks raw
vectors, identities, input order, scene witnesses and summary counts; it requires
all three native cases and rejects skipped tests. Inspect the exact commit and
conclusion in [qualification runs](https://github.com/LewisJor/temms/actions/workflows/ubuntu24.yml).
Local Mac results do not substitute for hosted Linux evidence.

The exact workflow validator accepted the local replay and rejected nine in-memory
corruptions of scores, identities, hashes, activation order and scene witnesses.
The original records were preserved. Workflow syntax and the Sphinx documentation
build with warnings treated as errors also passed.

Core remains **429 physical lines** across five modules, largest 178; exports 50,
adapters 229 and the tiny example 37. Required runtime dependencies remain zero.
Replay support grew from **363 to 477 lines** for reference preparation, validation
and reporting. No backend, protocol, lifecycle store or platform component was added.

## Historical hosted evidence and limits

The [Ubuntu qualification at `58936d5`](https://github.com/LewisJor/temms/actions/runs/38754444278)
passed on 2026-10-15 with **196 tests, zero skips**, including the two then-existing
native cases. It recorded Ubuntu **24.04.5 LTS x86_64**, CPython **3.12.15**, ONNX
Runtime **1.24.1**, NumPy **2.4.2**, Pillow **12.1.0** and ONNX **1.21.0**. The
installed wheel completed seven scenes in a non-root network namespace with only
loopback, no routes and a failed direct-IP probe (`ENETUNREACH`). Its 84 attempts
produced 69 predictions, three waits and 12 abstentions. This was the **single-photo
replay**, before the current numerical checks and input-coverage requirements.

Its artifact `ubuntu24-qualification-38754444278-1` retains environment details,
wheel/module hashes, tests, network evidence and raw records for 30 days on GitHub;
a local copy is preserved under `.demo-assets/live-swap/ubuntu24-ci-38754444278/`.
[General CI](https://github.com/LewisJor/temms/actions/runs/38754444328) and the
[installed core check](https://github.com/LewisJor/temms/actions/runs/38754443960)
also passed. The subsequent [documentation deployment](https://github.com/LewisJor/temms/actions/runs/38783174527)
published the maintained Markdown through Sphinx at the
[live site](https://lewisjor.github.io/temms/). Build tools stay separate from core.

Direct references share preprocessing and ONNX Runtime with serving, so agreement
checks integration consistency rather than independently validating either. Reference
setup warms the process; the paced replay is not a performance benchmark. The
[comparison results](../examples/live_swap/COMPARISON.md), wrapper-overhead findings
and rejected CPU-arena experiment remain dated historical measurements; they were
not rerun for this change. Physical-device latency, memory, energy, quality and
endurance remain unqualified. See [release/support criteria](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** perform the owner's unaided clean-clone first-use trial
using [README.md](../README.md) and the [demo runbook](../examples/live_swap/README.md),
recording the commit, environment and any friction.

Read `TEMMS.reconcile` in `src/temms/engine.py`, `OnnxRuntime.infer` in
`src/temms/adapters/onnx.py`, and `check_output` in `examples/live_swap/run.py`.
The hardest invariant: activation changes future requests, but an admitted request
must finish with its captured model's output and identity. Exercise without AI:
extend the controlled in-flight test so the old native request fails after the new
model publishes, and assert that the new model still serves afterward.
