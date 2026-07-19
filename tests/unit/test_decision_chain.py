"""Tamper-evident decision chain (issue #27).

The decision log ("which model ran, when, why") is hash-linked so any deletion,
reorder, or mutation is detectable offline, and its head is Ed25519-signed so an
auditor can confirm authenticity with the public key only — the provenance
guarantee that matters on a captured edge device.
"""

from __future__ import annotations

import json

from temms.core.signing import ed25519_verify, generate_ed25519_keypair
from temms.slots.manager import DECISION_CHAIN_GENESIS, SlotManager


def _seed(sm: SlotManager) -> None:
    sm.create_slot("vision", "Vision", default_model="a")
    for model, trigger in [("a", "startup"), ("b", "policy"), ("c", "policy"), ("b", "fallback")]:
        sm.activate_model(
            "vision", model, trigger, "seed",
            conditions={"visibility_m": 60}, audit_metadata={"model_id": model},
        )


def test_chain_links_and_verifies(slot_manager):
    _seed(slot_manager)
    result = slot_manager.verify_decision_chain()
    assert result["valid"] is True
    assert result["length"] == 4
    assert result["head_hash"] != DECISION_CHAIN_GENESIS
    # First entry links to genesis.
    rows = slot_manager.fetchall("SELECT * FROM slot_decisions ORDER BY id ASC")
    assert rows[0]["prev_hash"] == DECISION_CHAIN_GENESIS
    assert rows[1]["prev_hash"] == rows[0]["entry_hash"]


def test_mutation_is_detected(slot_manager):
    _seed(slot_manager)
    slot_manager.execute("UPDATE slot_decisions SET to_model='EVIL' WHERE id=2")
    slot_manager.conn.commit()
    result = slot_manager.verify_decision_chain()
    assert result["valid"] is False
    assert result["broken_at"] == 1
    assert "hash" in result["reason"]


def test_deletion_is_detected(slot_manager):
    _seed(slot_manager)
    slot_manager.execute("DELETE FROM slot_decisions WHERE id=2")
    slot_manager.conn.commit()
    result = slot_manager.verify_decision_chain()
    # The entry after the deleted one no longer links to its recorded prev_hash.
    assert result["valid"] is False
    assert result["reason"] == "prev_hash link mismatch"


def test_legacy_rows_are_backfilled(slot_manager):
    _seed(slot_manager)
    # Simulate rows written before the chain existed.
    slot_manager.execute("UPDATE slot_decisions SET entry_hash=NULL, prev_hash=NULL")
    slot_manager.conn.commit()
    slot_manager._backfill_decision_chain()
    result = slot_manager.verify_decision_chain()
    assert result["valid"] is True
    assert result["length"] == 4


def test_head_signature_verifies_offline_with_public_key(slot_manager):
    _seed(slot_manager)
    private_pem, public_pem, fingerprint = generate_ed25519_keypair()
    head = slot_manager.sign_decision_chain_head(private_pem, signer="hub")

    assert head["schema_version"] == "temms-decision-chain-head/v1"
    assert head["key_fingerprint"] == fingerprint
    assert head["length"] == 4
    # Verifies with the public key only (offline provenance).
    assert ed25519_verify(head["head_hash"].encode(), head["signature"], public_pem) is True
    # A different key does not verify.
    _, other_public, _ = generate_ed25519_keypair()
    assert ed25519_verify(head["head_hash"].encode(), head["signature"], other_public) is False


def test_tamper_after_signing_invalidates_the_signed_head(slot_manager):
    _seed(slot_manager)
    private_pem, public_pem, _ = generate_ed25519_keypair()
    head = slot_manager.sign_decision_chain_head(private_pem)

    # Adversary mutates a decision; the recomputed head no longer matches the
    # signed head, and the chain fails verification.
    slot_manager.execute("UPDATE slot_decisions SET trigger_detail='forged' WHERE id=3")
    slot_manager.conn.commit()
    assert slot_manager.verify_decision_chain()["valid"] is False
    # The signed head still verifies as a signature, but it no longer describes
    # the current (tampered) chain — detectable by comparing head hashes.
    assert ed25519_verify(head["head_hash"].encode(), head["signature"], public_pem) is True
    assert slot_manager.decision_chain_head() == head["head_hash"]  # head entry unchanged (id=4)
    # ...but an entry before the head was altered, so verify_decision_chain fails.


def test_exported_chain_verifies_offline_without_the_db(slot_manager):
    from temms.evidence import verify_decision_chain_export

    _seed(slot_manager)
    private_pem, public_pem, fingerprint = generate_ed25519_keypair()
    block = {
        "entries": slot_manager.export_decision_chain(),
        "head_signature": slot_manager.sign_decision_chain_head(private_pem),
    }

    result = verify_decision_chain_export(block, public_key=public_pem)
    assert result["valid"] is True
    assert result["length"] == 4
    assert result["signature_valid"] is True
    assert result["head_matches_signed_head"] is True
    assert result["key_fingerprint"] == fingerprint


def test_exported_chain_detects_tamper_offline(slot_manager):
    from temms.evidence import verify_decision_chain_export

    _seed(slot_manager)
    block = {"entries": slot_manager.export_decision_chain()}
    # Adversary edits an entry in the exported audit file.
    block["entries"][1]["to_model"] = "EVIL"
    result = verify_decision_chain_export(block)
    assert result["valid"] is False
    assert result["broken_at"] == 1


def test_conditions_and_audit_are_covered_by_the_hash(slot_manager):
    _seed(slot_manager)
    # Altering the recorded conditions snapshot must break the chain.
    slot_manager.execute(
        "UPDATE slot_decisions SET conditions_snapshot=? WHERE id=1",
        (json.dumps({"visibility_m": 9999}),),
    )
    slot_manager.conn.commit()
    assert slot_manager.verify_decision_chain()["valid"] is False


def test_exported_chain_verifies_against_a_trust_store(slot_manager):
    """Captured evidence verifies against a provisioned key set (#31)."""
    from temms.core.trust_store import load_trust_store_from_keys
    from temms.evidence import verify_decision_chain_export

    _seed(slot_manager)
    private_pem, public_pem, fingerprint = generate_ed25519_keypair()
    _, other_public, _ = generate_ed25519_keypair()
    block = {
        "entries": slot_manager.export_decision_chain(),
        "head_signature": slot_manager.sign_decision_chain_head(private_pem),
    }

    # The signer sits alongside an unrelated key, as it would mid-rotation.
    store = load_trust_store_from_keys([other_public, public_pem])
    result = verify_decision_chain_export(block, trust_store=store)

    assert result["valid"] is True
    assert result["signature_valid"] is True
    assert result["verified_by_fingerprint"] == fingerprint


def test_exported_chain_rejects_a_signer_outside_the_trust_store(slot_manager):
    """Evidence from an untrusted signer must not pass as verified."""
    from temms.core.trust_store import load_trust_store_from_keys
    from temms.evidence import verify_decision_chain_export

    _seed(slot_manager)
    private_pem, _, _ = generate_ed25519_keypair()
    _, stranger_public, _ = generate_ed25519_keypair()
    block = {
        "entries": slot_manager.export_decision_chain(),
        "head_signature": slot_manager.sign_decision_chain_head(private_pem),
    }

    store = load_trust_store_from_keys([stranger_public])
    result = verify_decision_chain_export(block, trust_store=store)

    assert result["valid"] is True  # the chain itself is intact...
    assert result["signature_valid"] is False  # ...but nobody trusted vouches for it
    assert "untrusted key" in result["signature_error"]
