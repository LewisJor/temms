"""Transport for Hub Lite commands.

Commands depend on the :class:`HubTransport` protocol, never on httpx. That is
the dependency-inversion seam: a command can be exercised with a fake transport
in a plain unit test, with no CLI runner, no monkeypatching of ``httpx.Client``,
and no network.

Commands never construct a transport. Construction is the CLI layer's job --
objects either wire collaborators together or do work, not both.
"""

from __future__ import annotations

from typing import Any, Protocol


class HubTransportError(RuntimeError):
    """The Hub returned an error response."""


class HubTransport(Protocol):
    """The only thing a Hub command may know about the network."""

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]: ...


class HttpHubTransport:
    """A :class:`HubTransport` backed by an httpx client.

    Takes an already-open client rather than building one, so the caller owns
    the connection lifetime (and tests can pass a stub).
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._checked(self._client.get(path, params=params))

    def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._checked(self._client.post(path, json=json))

    @staticmethod
    def _checked(response: Any) -> dict[str, Any]:
        """Turn an HTTP error response into an exception with a usable message."""
        if response.status_code >= 400:
            raise HubTransportError(f"HTTP {response.status_code}: {response.text}")
        return response.json()
