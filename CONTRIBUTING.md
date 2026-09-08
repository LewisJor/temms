# Contributing

TEMMS deliberately maintains a small surface. Before opening a change:

- State the one behavior the change adds or corrects.
- Explain why it belongs in the kernel rather than an application or adapter.
- Show two real consumers before introducing a new abstraction.
- Keep the runtime as the sole source of model access and active state.
- Add a focused test and run `pytest`.
- Run `ruff check src tests examples`.
- Prefer a smaller replacement over a compatibility layer.

Changes that add a Hub, daemon, HTTP service, database, fleet manager, registry,
or UI to the core will not be accepted.
