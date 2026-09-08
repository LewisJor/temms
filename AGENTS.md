# TEMMS implementation rules

TEMMS is a library, not a platform. AI coding agents must preserve these rules.

1. The injected runtime owns model access, active state, activation, and inference.
2. `ModelRef` is identity only; selection behavior belongs in `ModelPolicy` and `Selector`.
3. TEMMS never mutates or persists active-model state itself.
4. Core has zero required runtime dependencies and no server, daemon, database, registry, UI, or fleet code.
5. `Runtime` and `Selector` are the only public protocols. `TEMMS` is the only orchestration class.
6. One TEMMS instance owns reconciliation per runtime slot; same-slot calls are serialized.
7. Inference must remain pinned to and attributed to the model instance captured at admission.
8. Do not add an abstraction until two concrete consumers exist.
9. Do not add compatibility shims for the v0.1 prototype. Git history is the archive.
10. Prefer deleting code to generalizing it. Do not add speculative backends or features.
11. Keep every core file under 200 lines and total direct core code under 600 lines.
12. Every pull request must have one behavior-level purpose and keep the architecture budget green.
