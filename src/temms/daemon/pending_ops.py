from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from temms.core.atomic import write_json_atomic
from temms.core.signing import SIGNATURE_ALGORITHM, signing_key_fingerprint

PENDING_OPERATION_SIGNATURE_SCHEMA = "temms-pending-operation-signature/v1"


@dataclass
class PendingOperationsStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            write_json_atomic(self.path, [])


    def enqueue(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        signing_key: str | None = None,
        signer: str | None = None,
    ) -> None:
        entries = self.read_all()
        entry = {
            "operation": operation,
            "payload": payload,
            "recorded_at": datetime.now().isoformat(),
        }
        if signing_key:
            entry["signature"] = sign_pending_operation(
                entry,
                signing_key,
                signer=signer,
            )
        entries.append(entry)
        write_json_atomic(self.path, entries, indent=2)

    def read_all(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8"))


    def clear(self) -> None:
        write_json_atomic(self.path, [])

    def replace_all(self, entries: list[dict[str, Any]]) -> None:
        """Replace the active queue while preserving each entry unchanged."""
        write_json_atomic(self.path, entries, indent=2)






def sign_pending_operation(
    entry: dict[str, Any],
    signing_key: str,
    *,
    signer: str | None = None,
) -> dict[str, Any]:
    """Return tamper-evident signature metadata for one pending operation."""
    payload = _signature_payload(entry)
    return {
        "schema_version": PENDING_OPERATION_SIGNATURE_SCHEMA,
        "algorithm": SIGNATURE_ALGORITHM,
        "signed_at": datetime.now().isoformat(),
        "signer": signer or "temms-ddil",
        "key_fingerprint": signing_key_fingerprint(signing_key),
        "payload_sha256": _canonical_hash(payload),
        "signature": _signature_for_payload(payload, signing_key),
    }


def verify_pending_operation_signature(
    entry: dict[str, Any],
    signing_key: str,
) -> dict[str, Any]:
    """Verify one signed pending operation and return compact signature metadata."""
    signature = entry.get("signature")
    if not isinstance(signature, dict):
        raise ValueError("Pending operation is missing a signature")
    if signature.get("schema_version") != PENDING_OPERATION_SIGNATURE_SCHEMA:
        raise ValueError("Pending operation signature schema is not supported")
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        raise ValueError("Pending operation signature algorithm is not supported")

    payload = _signature_payload(entry)
    payload_sha256 = _canonical_hash(payload)
    if signature.get("payload_sha256") != payload_sha256:
        raise ValueError("Pending operation signature payload digest mismatch")
    expected_fingerprint = signing_key_fingerprint(signing_key)
    if signature.get("key_fingerprint") != expected_fingerprint:
        raise ValueError("Pending operation signature key fingerprint mismatch")

    expected = _signature_for_payload(payload, signing_key)
    actual = str(signature.get("signature") or "")
    if not hmac.compare_digest(expected, actual):
        raise ValueError("Pending operation signature mismatch")

    return {
        "schema_version": signature.get("schema_version"),
        "algorithm": signature.get("algorithm"),
        "signed_at": signature.get("signed_at"),
        "signer": signature.get("signer"),
        "key_fingerprint": expected_fingerprint,
        "payload_sha256": payload_sha256,
        "verified": True,
    }


def pending_operation_signature_status(
    entry: dict[str, Any],
    *,
    signing_key: str | None = None,
    require_signature: bool = False,
) -> dict[str, Any]:
    """Return non-secret verification status for a queued DDIL operation."""
    signature = entry.get("signature")
    if signing_key and isinstance(signature, dict):
        try:
            verified = verify_pending_operation_signature(entry, signing_key)
        except ValueError as exc:
            return {
                "status": "invalid",
                "verified": False,
                "reason": str(exc),
                **_signature_metadata(signature),
            }
        return {
            "status": "verified",
            "verified": True,
            "reason": "signature verified",
            **verified,
        }
    if isinstance(signature, dict):
        return {
            "status": "key_unavailable",
            "verified": False,
            "reason": "signature verification requires a signing key",
            **_signature_metadata(signature),
        }
    if require_signature:
        return {
            "status": "missing_signature",
            "verified": False,
            "reason": "signature required",
        }
    return {
        "status": "unsigned_allowed",
        "verified": False,
        "reason": "signature not required",
    }


def _signature_metadata(signature: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": signature.get("schema_version"),
        "algorithm": signature.get("algorithm"),
        "signed_at": signature.get("signed_at"),
        "signer": signature.get("signer"),
        "key_fingerprint": signature.get("key_fingerprint"),
        "payload_sha256": signature.get("payload_sha256"),
    }


def _normalize_payload_sha256(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("sha256:"):
        return text.removeprefix("sha256:").strip()
    return text


def _entry_payload_sha256(entry: dict[str, Any]) -> str:
    payload = entry.get("payload") if isinstance(entry, dict) else {}
    return _canonical_hash(payload if isinstance(payload, dict) else {})


def _signature_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": entry.get("operation"),
        "payload": entry.get("payload"),
        "recorded_at": entry.get("recorded_at"),
    }


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _signature_for_payload(payload: dict[str, Any], signing_key: str) -> str:
    return hmac.new(
        signing_key.encode("utf-8"),
        _canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
