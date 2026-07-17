# Tamper-Evident Decision Chain

See [`direction.md`](direction.md): an offline-verifiable, tamper-evident record
of *which model ran, when, and why* is the differentiated provenance capability
for DDIL/contested edge — the record must hold up even on a captured device.

## What it is

Every model activation is written to the decision log as a link in a
**hash chain**: each entry embeds the SHA-256 of the previous entry
(`prev_hash`) and its own content hash (`entry_hash`). The first entry links to
a genesis value (`0` × 64).

- **Deletion, reorder, or mutation** of any past decision breaks the chain and
  is detected — the recomputed `entry_hash` or the `prev_hash` link no longer
  matches.
- The chain head can be **Ed25519-signed**, so an auditor confirms the head is
  authentic **offline**, with the public key only (no CA, no transparency log —
  the same offline model as [package signing](package-signing.md)).
- Entry hashes use the canonical, number-normalized digest
  (`core.mission_package.canonical_json_hash`), so the chain verifies identically
  from any client or a future native port.

The hash covers the decision content that matters for audit: slot, from/to
model, trigger type + detail, the condition snapshot, the model/package
provenance audit metadata, and the timestamp.

## Where it lives

- `src/temms/slots/manager.py` — the chain is maintained in `activate_model`;
  `verify_decision_chain()` walks and validates it; `sign_decision_chain_head()`
  signs the head. Legacy rows written before the chain existed are backfilled on
  startup.
- `src/temms/core/signing.py` — `ed25519_sign` / `ed25519_verify` primitives.
- Evidence bundles carry a `decision_chain` block:

  ```json
  {
    "schema_version": "temms-decision-chain/v1",
    "head_hash": "…",
    "length": 128,
    "verification": { "valid": true, "length": 128, "head_hash": "…" },
    "head_signature": {
      "schema_version": "temms-decision-chain-head/v1",
      "head_hash": "…", "signed_at": "…", "signer": "…",
      "key_fingerprint": "ed25519:…", "signature": "<base64>"
    }
  }
  ```

  A recipient verifies the head signature with
  `ed25519_verify(head_hash.encode(), signature, public_key)`.

## Verifying

```python
from temms.slots.manager import SlotManager
from temms.core.signing import ed25519_verify

sm = SlotManager(data_dir / "temms.db")
result = sm.verify_decision_chain()          # {"valid": True/False, "length": N, ...}
head = sm.sign_decision_chain_head(private_key)
assert ed25519_verify(head["head_hash"].encode(), head["signature"], public_key)
```

## Threat model

- **Detects:** silent deletion, reordering, or editing of any decision on disk;
  a forged head that wasn't signed by a trusted key.
- **Does not by itself prevent:** an attacker who controls the daemon *at write
  time* and also holds the private key (that is the key-custody problem — keep
  the signing key off the edge and sign heads centrally, or on an HSM).

## Follow-ups (tracked)

- A portable, ordered full-chain export + a `temms` CLI to re-walk and verify a
  captured chain on a different machine with just the public key.
- Extend the chain/signature model to the other evidence streams (rollouts,
  DDIL intent queue) as they become audit-critical.
