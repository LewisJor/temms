# TEMMS progress — updated 2026-10-15

TEMMS remains an alpha Python library for application-owned local inference.
This publication brings the reviewed runtime contracts, real-model example,
installation checks and Ubuntu 24.04 qualification into one reproducible checkout.
The previous public baseline was `8b97dc5f34538fb4f67699778f4cc507bdcb641b`.
No server, registry, fleet service or additional inference engine was introduced.

## Current verification

Hosted results are tied to a commit. Inspect the latest completed run for `main`:

- [General CI](https://github.com/LewisJor/temms/actions/workflows/ci.yml): Python
  3.11/3.12 lint, strict typing, tests and package builds on Ubuntu 24.04.
- [Installed core package](https://github.com/LewisJor/temms/actions/workflows/package.yml):
  fresh dependency-free wheel installation, imports and the tiny example.
- [Ubuntu CPU qualification](https://github.com/LewisJor/temms/actions/workflows/ubuntu24.yml):
  installed-wheel tests and the real seven-scene replay with external networking
  unavailable. A configured workflow or uploaded diagnostic artifact alone is not
  a passing qualification. Check the run conclusion and its commit together.

The qualification profile is Ubuntu 24.04 x86_64, CPython 3.12.15, ONNX Runtime
1.24.1, NumPy 2.4.2 and Pillow 12.1.0; ONNX 1.21.0 builds test fixtures. It requires
zero skipped tests and both named native cases. Preparation verifies pinned model
assets online, then a direct-IP probe and replay share a new network namespace
under the ordinary runner UID. There is no connected fallback.

Pacing is declared as 12 attempts per phase at 0.5-second intervals, 84 attempts
across seven scenes. A single complete replay is required. Runner/OS/CPU details,
source and workflow revisions, wheel/module hashes, dependency versions, test
reports, network evidence and replay records are retained for 30 days. Preserve
accepted evidence before expiry. See the
[qualification runbook](../examples/live_swap/README.md#ubuntu-2404-qualification-in-github-actions).

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
uses `python -m pytest -ra`, matching the verified development command. All Actions
references use verified commit SHAs, read-only repository permissions and checkout
without persisted credentials. The library's production behavior did not change
in this publication cleanup.

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

A green hosted run establishes functional compatibility and prepared offline
operation for its recorded profile. It does not qualify physical-device latency,
peak memory, quality thresholds or endurance. Private security reporting, stable
compatibility commitments and customer support terms remain separate release gates
in [CONTRIBUTING.md](../CONTRIBUTING.md#release-and-support-criteria).

**Exactly one next action:** perform the owner's clean-clone first-use trial using
README.md and the demo runbook, recording the commit, environment and any friction.

Read `TEMMS.reconcile` in `src/temms/engine.py`, `OnnxRuntime.infer` in
`src/temms/adapters/onnx.py`, and `observe_scene` in `examples/live_swap/run.py`.
The hardest invariant: activation may change future requests, but an admitted
request must finish on and be attributed to its captured model. Exercise without AI:
predict a frame admitted before publication but completed afterward, then locate
the test that prevents it from counting as evidence of the new model serving.
