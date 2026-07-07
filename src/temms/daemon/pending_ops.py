from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

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
        if not self.dead_letter_path.exists():
            write_json_atomic(self.dead_letter_path, [])

    @property
    def dead_letter_path(self) -> Path:
        suffix = self.path.suffix or ".json"
        return self.path.with_name(f"{self.path.stem}_dead_letter{suffix}")

    def enqueue(
        self,
        operation: str,
        payload: Dict[str, Any],
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

    def read_all(self) -> List[Dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def read_dead_letter(self) -> List[Dict[str, Any]]:
        if not self.dead_letter_path.exists():
            return []
        return json.loads(self.dead_letter_path.read_text(encoding="utf-8"))

    def clear(self) -> None:
        write_json_atomic(self.path, [])

    def replace_all(self, entries: List[Dict[str, Any]]) -> None:
        """Replace the active queue while preserving each entry unchanged."""
        write_json_atomic(self.path, entries, indent=2)

    def retarget_runtime(
        self,
        *,
        payload_sha256: str,
        runtime_target_id: str,
        actor: str,
        reason: str,
        runtime_target_proof: Dict[str, Any] | None = None,
        signing_key: str | None = None,
        signer: str | None = None,
        require_signature: bool = False,
    ) -> Dict[str, Any]:
        """Rewrite one queued deploy intent to a new runtime target."""
        target_digest = _normalize_payload_sha256(payload_sha256)
        if not target_digest:
            raise ValueError("payload_sha256 is required")
        runtime_target_id = runtime_target_id.strip()
        if not runtime_target_id:
            raise ValueError("runtime_target_id is required")

        entries = self.read_all()
        updated_entries: list[Dict[str, Any]] = []
        retargeted: Dict[str, Any] | None = None
        retargeted_at = datetime.now().isoformat()

        for entry in entries:
            payload = entry.get("payload") if isinstance(entry, dict) else {}
            payload_dict = payload if isinstance(payload, dict) else {}
            current_digest = _canonical_hash(payload_dict)
            if current_digest != target_digest:
                updated_entries.append(entry)
                continue
            if retargeted is not None:
                raise ValueError("payload_sha256 matched multiple pending operations")
            if entry.get("operation") != "deploy":
                raise ValueError("only deploy pending operations can be runtime-retargeted")
            previous_runtime_target_id = str(payload_dict.get("runtime_target_id") or "")
            if previous_runtime_target_id == runtime_target_id:
                raise ValueError("pending deploy intent already uses requested runtime target")
            if (require_signature or isinstance(entry.get("signature"), dict)) and not signing_key:
                raise ValueError("retargeting this pending operation requires a signing key")

            audit_log = payload_dict.get("_temms_runtime_retarget")
            audit_records = list(audit_log) if isinstance(audit_log, list) else []
            retarget_record = {
                "schema_version": "temms-runtime-retarget/v1",
                "retargeted_at": retargeted_at,
                "actor": actor,
                "reason": reason,
                "previous_runtime_target_id": previous_runtime_target_id,
                "runtime_target_id": runtime_target_id,
                "previous_payload_sha256": current_digest,
            }
            if runtime_target_proof:
                retarget_record["runtime_target_proof"] = runtime_target_proof
            next_payload = {
                **payload_dict,
                "runtime_target_id": runtime_target_id,
                "_temms_runtime_retarget": [*audit_records, retarget_record],
            }
            updated_entry = {**entry, "payload": next_payload}
            if signing_key:
                updated_entry["signature"] = sign_pending_operation(
                    updated_entry,
                    signing_key,
                    signer=signer or actor,
                )
            updated_digest = _canonical_hash(next_payload)
            retargeted = {
                "retargeted": 1,
                "payload_sha256": current_digest,
                "updated_payload_sha256": updated_digest,
                "previous_runtime_target_id": previous_runtime_target_id,
                "runtime_target_id": runtime_target_id,
                "retargeted_at": retargeted_at,
                "actor": actor,
                "reason": reason,
                "runtime_target_proof": runtime_target_proof,
                "entry": updated_entry,
            }
            updated_entries.append(updated_entry)

        if retargeted is None:
            return {"retargeted": 0, "payload_sha256": target_digest}

        write_json_atomic(self.path, updated_entries, indent=2)
        return retargeted

    def quarantine(
        self,
        *,
        indexes: set[int],
        preflight_entries: Dict[int, Dict[str, Any]],
        actor: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Move selected pending operations to a dead-letter ledger."""
        entries = self.read_all()
        dead_letters = self.read_dead_letter()
        quarantined_at = datetime.now().isoformat()
        remaining: list[Dict[str, Any]] = []
        quarantined: list[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if index not in indexes:
                remaining.append(entry)
                continue
            preflight = preflight_entries.get(index, {})
            payload = entry.get("payload") if isinstance(entry, dict) else {}
            dead_letter = {
                "schema_version": "temms-pending-operation-dead-letter/v1",
                "quarantined_at": quarantined_at,
                "actor": actor,
                "reason": reason,
                "preflight": preflight,
                "payload_sha256": _canonical_hash(payload if isinstance(payload, dict) else {}),
                "entry": entry,
            }
            quarantined.append(dead_letter)
            dead_letters.append(dead_letter)
        write_json_atomic(self.dead_letter_path, dead_letters, indent=2)
        write_json_atomic(self.path, remaining, indent=2)
        return {
            "quarantined": len(quarantined),
            "remaining": len(remaining),
            "dead_letters": len(dead_letters),
            "entries": quarantined,
        }

    def acknowledge_dead_letters(
        self,
        *,
        actor: str,
        reason: str,
        payload_sha256s: set[str] | None = None,
    ) -> Dict[str, Any]:
        """Mark quarantined operations as handled while preserving audit history."""
        dead_letters = self.read_dead_letter()
        acknowledged_at = datetime.now().isoformat()
        acknowledged: list[Dict[str, Any]] = []
        updated_dead_letters: list[Dict[str, Any]] = []
        for record in dead_letters:
            if not isinstance(record, dict):
                updated_dead_letters.append(record)
                continue
            digest = str(record.get("payload_sha256") or "")
            matches_filter = payload_sha256s is None or digest in payload_sha256s
            already_acknowledged = bool(record.get("acknowledged"))
            already_requeued = bool(record.get("requeued"))
            if matches_filter and not already_acknowledged and not already_requeued:
                record = {
                    **record,
                    "acknowledged": True,
                    "acknowledged_at": acknowledged_at,
                    "acknowledged_by": actor,
                    "acknowledgement_reason": reason,
                }
                acknowledged.append(record)
            updated_dead_letters.append(record)
        write_json_atomic(self.dead_letter_path, updated_dead_letters, indent=2)
        return {
            "acknowledged": len(acknowledged),
            "dead_letters": len(updated_dead_letters),
            "entries": acknowledged,
        }

    def requeue_dead_letters(
        self,
        *,
        actor: str,
        reason: str,
        payload_sha256s: set[str] | None = None,
    ) -> Dict[str, Any]:
        """Move quarantined operations back to the active queue without losing audit."""
        entries = self.read_all()
        pending_hashes = {
            _entry_payload_sha256(entry)
            for entry in entries
            if isinstance(entry, dict)
        }
        dead_letters = self.read_dead_letter()
        requeued_at = datetime.now().isoformat()
        requeued: list[Dict[str, Any]] = []
        updated_dead_letters: list[Dict[str, Any]] = []

        for record in dead_letters:
            if not isinstance(record, dict):
                updated_dead_letters.append(record)
                continue
            digest = _normalize_payload_sha256(str(record.get("payload_sha256") or ""))
            matches_filter = payload_sha256s is None or digest in payload_sha256s
            entry = record.get("entry")
            entry_dict = entry if isinstance(entry, dict) else {}
            entry_digest = _entry_payload_sha256(entry_dict)
            already_requeued = bool(record.get("requeued"))
            already_acknowledged = bool(record.get("acknowledged"))
            already_pending = bool(entry_digest and entry_digest in pending_hashes)

            if (
                matches_filter
                and entry_dict
                and not already_requeued
                and not already_acknowledged
                and not already_pending
            ):
                entries.append(entry_dict)
                pending_hashes.add(entry_digest)
                record = {
                    **record,
                    "requeued": True,
                    "requeued_at": requeued_at,
                    "requeued_by": actor,
                    "requeue_reason": reason,
                }
                requeued.append(record)
            updated_dead_letters.append(record)

        write_json_atomic(self.path, entries, indent=2)
        write_json_atomic(self.dead_letter_path, updated_dead_letters, indent=2)
        return {
            "requeued": len(requeued),
            "pending": len(entries),
            "dead_letters": len(updated_dead_letters),
            "entries": requeued,
        }


def sign_pending_operation(
    entry: Dict[str, Any],
    signing_key: str,
    *,
    signer: str | None = None,
) -> Dict[str, Any]:
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
    entry: Dict[str, Any],
    signing_key: str,
) -> Dict[str, Any]:
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
    entry: Dict[str, Any],
    *,
    signing_key: str | None = None,
    require_signature: bool = False,
) -> Dict[str, Any]:
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


def _signature_metadata(signature: Dict[str, Any]) -> Dict[str, Any]:
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


def _entry_payload_sha256(entry: Dict[str, Any]) -> str:
    payload = entry.get("payload") if isinstance(entry, dict) else {}
    return _canonical_hash(payload if isinstance(payload, dict) else {})


def _signature_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "operation": entry.get("operation"),
        "payload": entry.get("payload"),
        "recorded_at": entry.get("recorded_at"),
    }


def _canonical_hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _signature_for_payload(payload: Dict[str, Any], signing_key: str) -> str:
    return hmac.new(
        signing_key.encode("utf-8"),
        _canonical_bytes(payload),
        hashlib.sha256,
    ).hexdigest()


def _canonical_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
