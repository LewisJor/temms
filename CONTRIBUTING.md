# Contributing

TEMMS is intentionally small. Before adding code, read `AGENTS.md` and
`docs/architecture.md`.

A contribution must:

- preserve runtime ownership of access, state, activation, and inference
- keep policy outside `ModelRef` and runtime adapters
- avoid required core dependencies and platform services
- add one behavior, not a speculative framework
- keep `tests/test_architecture_budget.py` green
- include tests for runtime truth and inference attribution where relevant

Run:

```bash
python -m pip install -e ".[dev,onnx]"
ruff check src tests examples
pytest
```
