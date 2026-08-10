"""What happens to a command's result: gates, rendering, files, exit code.

``hub()`` previously did this inline and re-branched on the action string seven
times to decide. Those branches were really per-action *policy*, so they belong
with the action, not in a shared tail: a command declares its emission policy,
and this module applies it uniformly.

Separating this from the commands keeps each side single-purpose -- a command
knows how to ask the Hub something, an emitter knows how to report it -- and
makes both testable without the other.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temms.cli.hub.commands import HubResult

Renderer = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class EmissionPolicy:
    """Per-action reporting rules, declared where the action is wired.

    ``failure_key`` replaces the old ``if action == "validate-runtime" and not
    payload.get("ok")`` special cases: an action states which payload field
    means failure, rather than the shared tail knowing every action's name.
    """

    renderer: Renderer | None = None
    # Payload key whose falsiness means the command failed (e.g. "ok").
    failure_key: str | None = None
    # Write the payload to --output when given.
    writes_payload_to_output: bool = False


class ResultEmitter:
    """Applies an :class:`EmissionPolicy` to a :class:`HubResult`.

    Holds only presentation concerns, injected rather than reached for, so a
    test can capture output without touching the console or the filesystem.
    """

    def __init__(
        self,
        *,
        console: Any,
        echo: Callable[[str], None],
        json_output: bool = False,
        output: Path | None = None,
    ) -> None:
        self._console = console
        self._echo = echo
        self._json_output = json_output
        self._output = output

    def emit(self, result: HubResult, policy: EmissionPolicy) -> bool:
        """Report the result. Returns True when the command should fail."""
        for message in result.messages:
            if not self._json_output:
                self._console.print(message)
        if result.handled:
            return False

        if policy.writes_payload_to_output and self._output is not None:
            self._write(self._output, result.payload)

        if self._json_output:
            self._echo(json.dumps(result.payload, indent=2, sort_keys=True))
        elif policy.renderer is not None:
            policy.renderer(result.payload)

        return self._failed(result, policy)

    def write_proof(self, proof_payload: dict[str, Any]) -> None:
        """Write an edge-runtime proof artifact to --output."""
        if self._output is None:
            return
        self._write(self._output, proof_payload)
        if not self._json_output:
            self._console.print(f"[green]Edge mission proof written:[/green] {self._output}")

    def report_gate_failures(self, failures: list[str]) -> None:
        if self._json_output:
            return
        for failure in failures:
            self._console.print(f"[red]Gate failed:[/red] {failure}")

    @staticmethod
    def _failed(result: HubResult, policy: EmissionPolicy) -> bool:
        if policy.failure_key is None:
            return False
        return not result.payload.get(policy.failure_key, True)

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if not self._json_output:
            self._console.print(f"[green]Written:[/green] {path}")
