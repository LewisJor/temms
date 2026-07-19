# Offline Trust Store and Key Rotation

TEMMS verifies signatures against a **local set of Ed25519 public keys**. There
is no CA to call and no transparency log to consult — both assume connectivity
a denied environment does not have. Trust is provisioned ahead of time, as a
file, and verification works on a device that will never phone home again.

Verification succeeds if **any** trusted, unexpired key validates the signature,
and the result records *which* key did. That is what makes evidence answer "who
signed this", not merely "this was signed".

## Where it lives

A JSON file, by default `/var/lib/temms/trust-store.json`:

```json
{
  "schema_version": "temms-trust-store/v1",
  "keys": [
    {
      "fingerprint": "ed25519:2d19cddd3c8e70fa",
      "public_key": "-----BEGIN PUBLIC KEY-----\n...",
      "label": "fleet-2027",
      "added_at": "2026-07-18T20:11:04+00:00",
      "expires_at": null
    }
  ]
}
```

Plain JSON on purpose: it can be baked into an image, pushed by config
management, or hand-copied onto an air-gapped device. Writes are atomic, so an
interrupted update cannot truncate the file and leave a device trusting nothing.

## Managing keys

```bash
# Trust a signer
temms trust add fleet-2027.public.pem --label "fleet-2027"

# Optional expiry — the key stops being accepted after this instant
temms trust add fleet-2027.public.pem --label "fleet-2027" \
  --expires-at 2028-01-01T00:00:00Z

temms trust list      # fingerprints, labels, active/expired, expiry
temms trust remove ed25519:2d19cddd3c8e70fa
```

Fingerprints are derived from the **public** key, so the signer and every
verifier compute the same value. `temms keys fingerprint` prints it for a key
file of either kind.

**Only Ed25519 public keys may enter the store.** A private key is refused
outright: the store is provisioned onto edge devices, which are the machines
most likely to be captured, and shipping signing material there would hand an
adversary the ability to forge the very packages the store exists to
authenticate. A legacy HMAC secret is refused for the same reason — every holder
of a shared secret can forge.

The store is also validated when loaded. An unknown `schema_version`, or an
entry whose recorded fingerprint does not match its own key material, is a hard
error rather than a key that silently fails to verify later.

## Rotating a key offline

The store holds many keys at once, which is the whole point: there is never a
window where already-signed packages stop verifying.

1. **Generate** the new keypair — `temms keys generate --out-dir ./fleet-2027`.
2. **Distribute trust first.** Add the new *public* key to every edge while the
   old key is still trusted. Both are now accepted.
3. **Switch signing** to the new private key at the Hub. New packages verify on
   any edge that completed step 2; older packages still verify under the old key.
4. **Retire** the old key with `temms trust remove <fingerprint>` once the fleet
   has caught up.

Step 2 before step 3 is the ordering that matters. Reverse them and edges that
have not yet received the new key will reject freshly signed packages — with no
network to fix it over.

For a scheduled cutover, `--expires-at` on the outgoing key retires it
automatically at a known instant instead of relying on a follow-up sweep.

## Verifying against the store

Captured evidence, verified with public keys only:

```bash
temms evidence --input evidence.json --verify-chain --trust-store trust-store.json
# Decision chain intact — 7 entries, head 5e6d00277d07224b…
# Head signature verified (signer ed25519:2d19cddd3c8e70fa — edge-sim-current)
```

Programmatically, for packages:

```python
from temms.core.signing import verify_package_signature_with_trust_store
from temms.core.trust_store import TrustStore

store = TrustStore.load(Path("/var/lib/temms/trust-store.json"))
result = verify_package_signature_with_trust_store(package_dir, store)
result["verified_by_fingerprint"]   # which key vouched for it
result["verified_by_label"]
```

## Failure modes

Each refusal says what to do about it, because the operator may be offline:

| Situation | Result |
|---|---|
| Signer not in the store | `signature names untrusted key <fp>; add it with 'temms trust add' if it is legitimate` |
| Signer present but expired | `signing key <fp> expired at <ts>; rotate to a current key` |
| Store empty | `trust store is empty; provision a public key with 'temms trust add'` |
| Signature does not match | `signature did not verify against any trusted key` |

All of these exit `2` from the CLI, the same as a broken chain.

Two deliberate properties:

- **Expiry is evaluated at verification time**, not signing time. An expired key
  is refused even for material it signed while valid — the conservative reading,
  since an expired key is often a compromised or decommissioned one.
- **Trust selection never weakens integrity.** The store only decides *which key
  to try*. Manifest and file-hash checks run exactly as they do on the
  single-key path, so a tampered package fails regardless of who signed it.

## Relationship to package signing

[Package signing](package-signing.md) covers how packages are signed and the
single-key verification path, which is unchanged. The trust store sits in front
of it for multi-key deployments. Trust-store verification is **Ed25519 only** —
legacy HMAC packages are refused, since a shared secret cannot express "many
independent signers" and gives every holder the power to forge.

See also [the evidence chain](evidence-chain.md) for what is being signed.
