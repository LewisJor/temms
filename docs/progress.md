# TEMMS progress — updated 2026-10-15

TEMMS remains an alpha Python library for application-owned local inference.
This publication brings the reviewed runtime contracts, real-model example,
installation checks and Ubuntu 24.04 qualification into one reproducible checkout.
The previous public baseline was `8b97dc5f34538fb4f67699778f4cc507bdcb641b`.
No server, registry, fleet service or additional inference engine was introduced.

## Current verification

Published commit [`58936d50756b1ed96fbc8d46f716f74194ee5ce5`](https://github.com/LewisJor/temms/commit/58936d50756b1ed96fbc8d46f716f74194ee5ce5)
passed all three hosted workflows on **2026-10-15**:

- [General CI, run 38754444328](https://github.com/LewisJor/temms/actions/runs/38754444328):
  Python 3.11/3.12 lint, strict typing, tests and package builds on Ubuntu 24.04.
- [Installed core package, run 38754443960](https://github.com/LewisJor/temms/actions/runs/38754443960):
  fresh core-only wheel installation, imports and the tiny example.
- [Ubuntu CPU qualification, run 38754444278](https://github.com/LewisJor/temms/actions/runs/38754444278):
  **196 tests passed, zero skipped**, including both real ONNX cases; the installed
  wheel completed all seven replay scenes with external networking unavailable.

The qualification recorded Ubuntu **24.04.5 LTS x86_64**, CPython **3.12.15**, ONNX
Runtime **1.24.1**, NumPy **2.4.2** and Pillow **12.1.0**; ONNX **1.21.0** built test
fixtures. Independent artifact inspection matched the wheel's nine Python modules
to the published commit. The direct-IP probe and replay shared a new network
namespace under UID 1001, with only loopback and no routes; the probe returned
Linux `ENETUNREACH` (101). There was no connected fallback.

The declared 12 attempts per phase at 0.5-second intervals produced **84 attempts:
69 predictions, three waiting frames and 12 abstentions**. Ordered raw records
independently confirmed all seven scene witnesses, including post-failure continuity
and recovery. This one-photo replay does not measure classification quality.

The qualification artifact, `ubuntu24-qualification-38754444278-1`, contains
runner/OS/CPU details, source and workflow revisions, wheel/module hashes, dependency
versions, test reports, network evidence and replay records. GitHub retention is
30 days. A downloaded copy is preserved locally under the ignored
`.demo-assets/live-swap/ubuntu24-ci-38754444278/`; it is not included in a clone.
Check each later run's conclusion and commit together. See the
[qualification runbook](../examples/live_swap/README.md#ubuntu-2404-qualification-in-github-actions).

An automated first-use smoke check also passed from a fresh HTTPS clone of the
published commit, following the README installation and tiny-example commands.
The owner's unaided first-use trial remains outstanding.

## Publication review

The retained changes have specific purposes:

- Preserve failed-activation continuity, admission-pinned inference, cancellation
  boundaries and per-slot reconciliation ownership with maintained tests.
- Reject an in-memory activation without a registered handler before changing state.
- Remove the single-use reconciliation helper while preserving its transaction.
- Repair the typed public result contract and add strict downstream consumer checks.
- Keep serving dependencies separate from graph-authoring/development tools; include
  the full existing Apache-2.0 license and verify installed packages.
- Provide a pinned two-model terminal example, honest comparison protocols and
  explicit preparation/offline operation. Generated assets and local evidence are
  excluded from Git.

The first-use README now starts with clone, installation and a small executable
example. The example runbook uses a regular serving installation. Historical local
results are labeled as such; the repository does not imply that their raw records
or videos are included in a clone.

Final workflow review reproduced a real clean-environment collection failure with
console `pytest`: it did not expose the checkout's `examples` namespace. CI now
uses `python -m pytest -ra`, matching the verified development command. Actions
references use verified commit SHAs; build jobs have read-only repository access,
and checkout does not persist credentials. Only the Pages deployment job receives
`pages: write` and `id-token: write`. The library's production behavior did not change
in this publication cleanup.

The documentation publication path now builds the same eight maintained Markdown
files into one Sphinx site, covering installation, architecture, the demo and
comparisons, progress, contribution rules, the next action and agent instructions.
Its pinned tools live separately in `docs/requirements.txt`; core dependencies are
unchanged. A local build with warnings treated as errors passed. This update adds
the [Documentation workflow](../.github/workflows/docs.yml) to publish verified
builds from `main`. Check the [deployment runs](https://github.com/LewisJor/temms/actions/workflows/docs.yml)
and [live site](https://lewisjor.github.io/temms/) together: a local build alone does
not establish publication.

## Local evidence and scope

The 2026-10-15 current-source check on macOS ARM64/Python 3.11.8 passed **196 tests,
zero skipped**, strict mypy (**10 files**) and scoped Ruff. Earlier installed-wheel
Python 3.12.15 tests also passed 196 cases without skips. These Mac results do not
substitute for the hosted Linux run identified above.

The earlier local preflight used the qualification pacing and completed all seven
scenes across 84 attempts. Six in-memory checks confirmed the workflow rejects
skipped/failed tests, missing native coverage, incomplete verdicts, missing scenes
and duplicate summaries. The workflow shell/Python syntax and architecture checks
passed. Core remains **429 physical lines** across five modules, largest 178;
exports 50, adapters 229, tiny example 37. Required runtime dependencies remain zero.

The [runtime assessment](architecture.md#runtime-choice-and-long-term-fit) keeps
ONNX Runtime as the proven implementation and evaluates alternatives conditionally.
[COMPARISON.md](../examples/live_swap/COMPARISON.md) retains narrow local task results,
inconclusive wrapper overhead and the rejected CPU-arena experiment. Raw historical
records remain in ignored development directories and are not public release assets.
Performance experiments were not rerun for this publication.

The successful hosted run establishes functional compatibility and prepared offline
operation for its recorded profile. It does not qualify physical-device latency,
peak memory, quality thresholds or endurance. Private security reporting, stable
compatibility commitments and customer support terms remain separate release gates
in [CONTRIBUTING.md](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** perform the owner's unaided clean-clone first-use trial
using [README.md](../README.md) and the [demo runbook](../examples/live_swap/README.md),
recording the commit, environment and any friction.

Read `TEMMS.reconcile` in `src/temms/engine.py`, `OnnxRuntime.infer` in
`src/temms/adapters/onnx.py`, and `observe_scene` in `examples/live_swap/run.py`.
The hardest invariant: activation may change future requests, but an admitted
request must finish on and be attributed to its captured model. Exercise without AI:
predict a frame admitted before publication but completed afterward, then locate
the test that prevents it from counting as evidence of the new model serving.
