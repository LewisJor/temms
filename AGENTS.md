# TEMMS implementation rules

TEMMS is a library, not a platform. AI coding agents must preserve these rules.

1. The injected runtime owns model access, active state, activation, and inference.
2. TEMMS never mutates or persists active-model state itself.
3. Core has zero runtime dependencies and no network, server, daemon, database, registry, UI, or fleet code.
4. `Runtime` and `Selector` are the only public protocols. `TEMMS` is the only orchestration class.
5. Do not add an abstraction until two concrete consumers exist.
6. Do not add compatibility shims for the v0.1 prototype. Git history is the archive.
7. Prefer deleting code to generalizing it. Do not add speculative backends or features.
8. Keep every core file under 200 lines and total core code under 600 lines.
9. A runtime activation is successful only when subsequent runtime state reports the selected model identity.
10. Every pull request must have one behavior-level purpose and must keep `tests/test_architecture_budget.py` green.
