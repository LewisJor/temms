# Contributing

TEMMS is intentionally small. Before adding code, read [AGENTS.md](AGENTS.md),
[architecture](docs/architecture.md), and the current [progress record](docs/progress.md).

A contribution must:

- preserve runtime ownership of access, state, activation, and inference
- keep policy outside `ModelRef` and runtime adapters
- avoid required core dependencies and platform services
- add one behavior, not a speculative framework
- keep `tests/test_architecture_budget.py` green
- include tests for runtime truth and inference attribution where relevant

Use Python 3.11+ and an isolated environment. First [clone the repository](README.md#install-and-try),
then run these commands from its root (reuse the active environment if already created):

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,onnx,demo]"
python -m ruff check src tests examples
python -m mypy
python -m pytest -ra
python -m build
```

CI uses Ubuntu 24.04 and runs lint, strict type checking and tests on Python 3.11
and 3.12, installing the dev, onnx, and demo extras. The type check includes a
downstream consumer fixture with accepted and deliberately rejected uses of the
public runtime contract.
The `dev` extra includes the ONNX graph-building package used by native integration
fixtures; the `onnx` extra supplies only the inference dependencies. Install both
for native tests, and use only `onnx` (plus `demo` for the example) on a serving device.
On 3.11 it builds with both the isolated backend and the minimum setuptools 77.0.3.
Keep that minimum check aligned with `build-system.requires` in `pyproject.toml`.
Real ONNX tests can skip if optional packages are missing; report skips
explicitly rather than claiming ONNX coverage. See
[the workflow](.github/workflows/ci.yml) for the authoritative configuration.

The separate [Ubuntu 24.04 qualification](.github/workflows/ubuntu24.yml) uses an
installed wheel, CPython 3.12.15 and explicitly pinned inference dependencies. It
requires the full suite with zero skips and all three named native integration cases,
then runs the real seven-scene example inside a verified network namespace. The
[runbook](examples/live_swap/README.md#ubuntu-2404-qualification-in-github-actions)
defines acceptance and evidence retention. The [verification record](docs/progress.md)
identifies completed runs and their exact revisions. Rerun qualification when code
or dependencies change; a hosted pass does not establish performance on a physical
edge device. The separate [Webots camera qualification](.github/workflows/webots.yml)
checks rendered inputs, live swaps and native movie decoding on the declared Linux
profile; follow its [tutorial](examples/webots/README.md) to reproduce it.

For distribution changes, check the wheel after the build above in a fresh
environment outside the source tree:

```bash
temms_checkout_dir="$PWD"
temms_trial_dir="$(mktemp -d)"
python -m venv "$temms_trial_dir/venv"
"$temms_trial_dir/venv/bin/python" -m pip install --no-index --no-deps "$temms_checkout_dir/dist/temms-0.2.0a1-py3-none-any.whl"
(cd "$temms_trial_dir" && "$temms_trial_dir/venv/bin/python" -I "$temms_checkout_dir/examples/adaptive_vision.py")
"$temms_trial_dir/venv/bin/python" -I -c "import temms; print(temms.__file__)"
```

The example should print the same two lines as the README, and the import path
should be inside the temporary environment. Python's `-I` flag excludes the checkout
and `PYTHONPATH` from imports. The [package workflow](.github/workflows/package.yml)
also checks that NumPy, ONNX, ONNX Runtime and Pillow are absent and that package
metadata has no required runtime dependencies. The wheel filename includes the
current alpha version; update it when testing another version.

An isolated build needs access to its build requirements. If those dependencies
are already installed but network access is unavailable, `python -m build
--no-isolation` is an alternative; report that distinction with the result.
The installed backend must satisfy `setuptools>=77.0.3`: the project's SPDX license
metadata needs a compatible backend. This follows the
[Python packaging guidance](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#declaring-the-build-backend).

The architecture test also inspects directories in `src/temms`. Old ignored
prototype directories containing only bytecode can fail it in a reused checkout.
Inspect them before any cleanup; preserve user files and verify a clean checkout
or a temporary copy of tracked source. Do not weaken the test or silently report
the original checkout as passing. Always inspect the checkout being tested.

Keep one behavior-level purpose per PR, use bounded events/barriers for concurrency
tests, and record the hypothesis, actual checks, complexity change, and one next
action in `docs/progress.md`. A shared conformance helper is useful only where the
existing runtimes have genuinely overlapping contracts. No new testing framework
or production abstraction is needed merely to make their tests look alike.

## Documentation

The [public documentation](https://lewisjor.github.io/temms/) is built directly from
the maintained Markdown files listed in `docs/conf.py`; `index.rst` supplies the
site navigation. Edit those source files rather than generated HTML. The
[documentation workflow](.github/workflows/docs.yml) builds on pull requests and
publishes successful builds from `main`. Warnings and unresolved document links
fail the build.

Documentation tools require Python 3.12 or newer and are separate from the library's
dependencies. To build and preview from the repository root on macOS/Linux:

```bash
python3.12 -m venv .venv-docs
.venv-docs/bin/python -m pip install -r docs/requirements.txt
.venv-docs/bin/python -m sphinx -W --keep-going -b html -c docs . build/docs
.venv-docs/bin/python -m http.server --directory build/docs 8000
```

Open `http://localhost:8000` and check installation instructions, navigation and
links before publishing. `build/docs` is generated and ignored by Git.

## Release and support criteria

A passing checkout is not a supported customer release. Before publishing or
promising support, the maintainer should establish each of the following:

- Build from an identified reviewed commit, run the full checks above, inspect
  native-test skips, and verify the installed distribution. The separate
  [package workflow](.github/workflows/package.yml) installs only the wheel in an
  isolated environment, checks dependency metadata and optional-import boundaries,
  and runs the maintained tiny example. A configured job is not proof it has run.
- Name the supported OS/architecture, Python and inference versions. Check the
  claimed lower dependency bounds as well as the chosen deployment versions;
  a permissive requirement is not compatibility evidence. Preserve wheel/source
  hashes and the complete tested dependency versions with the release evidence.
  Test the prepared workload with networking denied and declared memory, latency
  and quality limits on that target, including failed replacements and repeated
  transitions over the application's expected operating duration.
- Include the full project license in wheel/source distributions and retain
  applicable dependency/model/data notices in any bundled deployment. The demo's
  pinned artifact terms do not establish rights for arbitrary customer models.
- Define supported releases and compatibility/deprecation expectations before
  offering a support contract. The current `0.2.0a1` designation is alpha; there
  is no established stable-API or response-time commitment.
- Establish and verify a private vulnerability-reporting route, then document it
  with supported versions and a responsible maintainer in `SECURITY.md`. Do not
  invent a contact address, invite sensitive public reports, or claim that a
  policy file enables the channel. GitHub's setting is separate.
  [Private reporting guidance](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
- Review release-workflow permissions and dependencies before enabling publication.
  Keep third-party Actions pinned to verified full commit SHAs for immutable
  action references, as the current workflows do.
  [GitHub workflow guidance](https://docs.github.com/en/actions/reference/security/secure-use)

These are release acceptance criteria, not new core services or a claim that the
current repository satisfies them. Keep unresolved status and one next action in
`docs/progress.md`; update this checklist when a real release changes the process.
