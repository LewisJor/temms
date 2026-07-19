"""Offline Ed25519 trust store (issue #31).

DDIL edges cannot reach a CA or a transparency log, so trust is provisioned as a
local set of Ed25519 public keys. Verification succeeds if *any* trusted,
unexpired key validates the signature, which is what makes key rotation possible
offline: during a transition both the outgoing and incoming keys sit in the
store, packages signed by either verify, and the outgoing key is dropped once
the fleet has caught up.

The store is a plain JSON file so it can be provisioned by config management,
baked into an image, or hand-copied onto a device that will never phone home.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temms.core.atomic import write_json_atomic
from temms.core.signing import signing_key_fingerprint

TRUST_STORE_SCHEMA_VERSION = "temms-trust-store/v1"


class TrustStoreError(ValueError):
    """The trust store could not satisfy a verification or management request."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: str, label: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing Z."""
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TrustStoreError(f"{label} is not a valid ISO-8601 timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass
class TrustedKey:
    """One provisioned Ed25519 public key."""

    fingerprint: str
    public_key: str
    label: str = ""
    added_at: str = ""
    expires_at: str | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if not self.expires_at:
            return False
        return _parse_timestamp(self.expires_at, "expires_at") <= (now or _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "public_key": self.public_key,
            "label": self.label,
            "added_at": self.added_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrustedKey:
        return cls(
            fingerprint=data["fingerprint"],
            public_key=data["public_key"],
            label=data.get("label", ""),
            added_at=data.get("added_at", ""),
            expires_at=data.get("expires_at"),
        )


@dataclass
class TrustStore:
    """A set of trusted Ed25519 public keys, keyed by fingerprint."""

    keys: dict[str, TrustedKey] = field(default_factory=dict)

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> TrustStore:
        """Load a trust store, returning an empty one if the file is absent."""
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TrustStoreError(f"trust store at {path} is not valid JSON: {exc}") from exc

        entries = data.get("keys", []) if isinstance(data, dict) else []
        keys: dict[str, TrustedKey] = {}
        for entry in entries:
            try:
                trusted = TrustedKey.from_dict(entry)
            except (KeyError, TypeError) as exc:
                raise TrustStoreError(f"malformed trust store entry in {path}: {entry}") from exc
            keys[trusted.fingerprint] = trusted
        return cls(keys=keys)

    def save(self, path: Path) -> None:
        """Write the store atomically so a crash cannot truncate trust."""
        payload = {
            "schema_version": TRUST_STORE_SCHEMA_VERSION,
            "keys": [key.to_dict() for key in self.sorted_keys()],
        }
        write_json_atomic(path, payload, indent=2, sort_keys=True)

    # -- management --------------------------------------------------------

    def sorted_keys(self) -> list[TrustedKey]:
        return sorted(self.keys.values(), key=lambda k: k.fingerprint)

    def add(
        self,
        public_key: str,
        *,
        label: str = "",
        expires_at: str | None = None,
        added_at: str | None = None,
    ) -> TrustedKey:
        """Trust a public key. Re-adding a fingerprint updates its metadata."""
        fingerprint = signing_key_fingerprint(public_key)
        if expires_at:
            _parse_timestamp(expires_at, "expires_at")
        trusted = TrustedKey(
            fingerprint=fingerprint,
            public_key=public_key.strip(),
            label=label,
            added_at=added_at or _utc_now().isoformat(),
            expires_at=expires_at,
        )
        self.keys[fingerprint] = trusted
        return trusted

    def remove(self, fingerprint: str) -> TrustedKey:
        """Stop trusting a key. Raises if it was not trusted."""
        trusted = self.keys.pop(fingerprint, None)
        if trusted is None:
            raise TrustStoreError(f"no trusted key with fingerprint {fingerprint}")
        return trusted

    def get(self, fingerprint: str) -> TrustedKey | None:
        return self.keys.get(fingerprint)

    def active_keys(self, now: datetime | None = None) -> list[TrustedKey]:
        """Trusted keys that have not expired."""
        moment = now or _utc_now()
        return [key for key in self.sorted_keys() if not key.is_expired(moment)]

    def __len__(self) -> int:
        return len(self.keys)

    # -- verification ------------------------------------------------------

    def candidates_for(
        self,
        declared_fingerprint: str | None,
        now: datetime | None = None,
    ) -> list[TrustedKey]:
        """Trusted keys worth trying for a signature.

        When the signature names its signer we try that key alone — trying the
        rest would only ever produce a confusing error. Expiry is enforced by
        the caller so an expired-but-present signer yields a precise message
        rather than a generic "no trusted key" one.
        """
        if declared_fingerprint:
            trusted = self.keys.get(declared_fingerprint)
            return [trusted] if trusted else []
        return self.active_keys(now)

    def verify_with_any(
        self,
        verifier: Any,
        declared_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> TrustedKey:
        """Return the first trusted key for which ``verifier(public_key)`` holds.

        ``verifier`` takes a PEM public key and returns True (or raises) — this
        keeps the store independent of what is being verified: packages,
        decision-chain heads, and queued intents all reuse it.
        """
        moment = now or _utc_now()
        if not self.keys:
            raise TrustStoreError(
                "trust store is empty; provision a public key with 'temms trust add'"
            )

        candidates = self.candidates_for(declared_fingerprint, moment)
        if declared_fingerprint and not candidates:
            raise TrustStoreError(
                f"signature names untrusted key {declared_fingerprint}; "
                "add it with 'temms trust add' if it is legitimate"
            )

        expired: list[str] = []
        for trusted in candidates:
            if trusted.is_expired(moment):
                expired.append(trusted.fingerprint)
                continue
            try:
                if verifier(trusted.public_key):
                    return trusted
            except Exception:
                continue

        if expired:
            raise TrustStoreError(
                f"signing key {expired[0]} expired at "
                f"{self.keys[expired[0]].expires_at}; rotate to a current key"
            )
        raise TrustStoreError("signature did not verify against any trusted key")


def default_trust_store_path(data_dir: Path) -> Path:
    """Conventional trust store location inside the daemon data directory."""
    return data_dir / "trust-store.json"


def load_trust_store_from_keys(public_keys: Iterable[str]) -> TrustStore:
    """Build an in-memory store from raw PEM keys (tests, one-shot verifies)."""
    store = TrustStore()
    for public_key in public_keys:
        store.add(public_key)
    return store
