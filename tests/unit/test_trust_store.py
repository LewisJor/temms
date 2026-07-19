"""Offline multi-key trust store and key rotation (#31).

The property that matters for DDIL: an edge verifies against a provisioned set
of public keys with no CA and no transparency log, and a key can be rotated by
trusting the old and new keys at once — no window where signed packages stop
verifying.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from temms.core import signing
from temms.core.trust_store import (
    TRUST_STORE_SCHEMA_VERSION,
    TrustedKey,
    TrustStore,
    TrustStoreError,
    load_trust_store_from_keys,
)


@pytest.fixture
def package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(json.dumps({"package_id": "pkg-x", "name": "x"}))
    (pkg / "model.onnx").write_bytes(b"onnx-bytes")
    return pkg


@pytest.fixture
def old_key():
    return signing.generate_ed25519_keypair()


@pytest.fixture
def new_key():
    return signing.generate_ed25519_keypair()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# -- store management ------------------------------------------------------


def test_add_and_list_keys(old_key, new_key):
    store = TrustStore()
    store.add(old_key[1], label="old")
    store.add(new_key[1], label="new")

    assert len(store) == 2
    assert {k.fingerprint for k in store.sorted_keys()} == {old_key[2], new_key[2]}
    assert store.get(old_key[2]).label == "old"


def test_readding_a_key_updates_metadata_without_duplicating(old_key):
    store = TrustStore()
    store.add(old_key[1], label="first")
    store.add(old_key[1], label="second")

    assert len(store) == 1
    assert store.get(old_key[2]).label == "second"


def test_remove_unknown_fingerprint_is_an_error(old_key):
    store = TrustStore()
    store.add(old_key[1])
    with pytest.raises(TrustStoreError, match="no trusted key"):
        store.remove("ed25519:nope")


def test_round_trips_through_disk(tmp_path, old_key, new_key):
    path = tmp_path / "trust.json"
    store = TrustStore()
    store.add(old_key[1], label="old", expires_at="2030-01-01T00:00:00Z")
    store.add(new_key[1], label="new")
    store.save(path)

    payload = json.loads(path.read_text())
    assert payload["schema_version"] == TRUST_STORE_SCHEMA_VERSION

    reloaded = TrustStore.load(path)
    assert len(reloaded) == 2
    assert reloaded.get(old_key[2]).expires_at == "2030-01-01T00:00:00Z"
    assert reloaded.get(new_key[2]).label == "new"


def test_missing_store_loads_empty_rather_than_failing(tmp_path):
    assert len(TrustStore.load(tmp_path / "absent.json")) == 0


def test_corrupt_store_is_reported_clearly(tmp_path):
    path = tmp_path / "trust.json"
    path.write_text("{not json")
    with pytest.raises(TrustStoreError, match="not valid JSON"):
        TrustStore.load(path)


def test_invalid_expiry_is_rejected_at_add_time(old_key):
    store = TrustStore()
    with pytest.raises(TrustStoreError, match="ISO-8601"):
        store.add(old_key[1], expires_at="whenever")


# -- expiry ----------------------------------------------------------------


def test_expired_key_is_not_active(old_key):
    past = _iso(datetime.now(UTC) - timedelta(days=1))
    store = TrustStore()
    store.add(old_key[1], expires_at=past)

    assert len(store) == 1
    assert store.active_keys() == []


def test_future_expiry_stays_active(old_key):
    future = _iso(datetime.now(UTC) + timedelta(days=1))
    store = TrustStore()
    store.add(old_key[1], expires_at=future)
    assert len(store.active_keys()) == 1


def test_key_without_expiry_never_expires(old_key):
    assert TrustedKey(fingerprint="f", public_key=old_key[1]).is_expired() is False


# -- package verification --------------------------------------------------


def test_verifies_package_against_trusted_key(package, old_key):
    private_pem, public_pem, fingerprint = old_key
    signing.sign_package(package, private_pem, signer="hub")
    store = load_trust_store_from_keys([public_pem])

    result = signing.verify_package_signature_with_trust_store(package, store)

    assert result["algorithm"] == "Ed25519"
    assert result["verified_by_fingerprint"] == fingerprint


def test_records_which_key_verified(package, old_key, new_key):
    """Evidence must answer *who* signed, not merely that it was signed."""
    signing.sign_package(package, new_key[0], signer="hub")
    store = TrustStore()
    store.add(old_key[1], label="retired")
    store.add(new_key[1], label="current")

    result = signing.verify_package_signature_with_trust_store(package, store)

    assert result["verified_by_fingerprint"] == new_key[2]
    assert result["verified_by_label"] == "current"


def test_untrusted_signer_is_rejected(package, old_key, new_key):
    signing.sign_package(package, new_key[0])
    store = load_trust_store_from_keys([old_key[1]])

    with pytest.raises(TrustStoreError, match="untrusted key"):
        signing.verify_package_signature_with_trust_store(package, store)


def test_empty_store_gives_actionable_error(package, old_key):
    signing.sign_package(package, old_key[0])
    with pytest.raises(TrustStoreError, match="trust store is empty"):
        signing.verify_package_signature_with_trust_store(package, TrustStore())


def test_expired_signer_is_rejected_with_a_clear_message(package, old_key):
    signing.sign_package(package, old_key[0])
    store = TrustStore()
    store.add(old_key[1], expires_at=_iso(datetime.now(UTC) - timedelta(days=1)))

    with pytest.raises(TrustStoreError, match="expired at"):
        signing.verify_package_signature_with_trust_store(package, store)


def test_tampered_package_still_fails_under_trust_store(package, old_key):
    """The trust store selects a key; it must not weaken integrity checks."""
    signing.sign_package(package, old_key[0])
    store = load_trust_store_from_keys([old_key[1]])
    (package / "model.onnx").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="hashes do not match"):
        signing.verify_package_signature_with_trust_store(package, store)


def test_hmac_package_is_refused_by_the_trust_store(package):
    """Trust-store verification is asymmetric-only by design."""
    signing.sign_package(package, "legacy-shared-secret")
    store = TrustStore()
    store.add(signing.generate_ed25519_keypair()[1])

    with pytest.raises(ValueError, match="requires an Ed25519 signature"):
        signing.verify_package_signature_with_trust_store(package, store)


# -- rotation --------------------------------------------------------------


def test_rotation_window_verifies_both_old_and_new_signers(tmp_path, old_key, new_key):
    """The point of the store: no gap where signed packages stop verifying."""
    old_pkg = tmp_path / "old-pkg"
    old_pkg.mkdir()
    (old_pkg / "manifest.json").write_text(json.dumps({"package_id": "p1"}))
    signing.sign_package(old_pkg, old_key[0])

    new_pkg = tmp_path / "new-pkg"
    new_pkg.mkdir()
    (new_pkg / "manifest.json").write_text(json.dumps({"package_id": "p2"}))
    signing.sign_package(new_pkg, new_key[0])

    store = load_trust_store_from_keys([old_key[1], new_key[1]])

    assert (
        signing.verify_package_signature_with_trust_store(old_pkg, store)[
            "verified_by_fingerprint"
        ]
        == old_key[2]
    )
    assert (
        signing.verify_package_signature_with_trust_store(new_pkg, store)[
            "verified_by_fingerprint"
        ]
        == new_key[2]
    )


def test_dropping_the_old_key_completes_rotation(tmp_path, old_key, new_key):
    old_pkg = tmp_path / "old-pkg"
    old_pkg.mkdir()
    (old_pkg / "manifest.json").write_text(json.dumps({"package_id": "p1"}))
    signing.sign_package(old_pkg, old_key[0])

    store = load_trust_store_from_keys([old_key[1], new_key[1]])
    store.remove(old_key[2])

    with pytest.raises(TrustStoreError, match="untrusted key"):
        signing.verify_package_signature_with_trust_store(old_pkg, store)
