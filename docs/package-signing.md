# Package Signing & Provenance

TEMMS packages are signed so an edge device can prove a package is authentic and
unmodified before loading it. See [`direction.md`](direction.md) for why this is
part of the differentiated core.

## Asymmetric by default (Ed25519)

The Hub signs a package with an **Ed25519 private key**; an edge daemon verifies
with the **public key only**. A device that can verify a package therefore
cannot forge one — the property that makes provenance meaningful when the device
is in someone else's hands.

Verification is **fully offline**: the daemon checks the signature against a
provisioned public key with no network, no certificate authority, and no
transparency log. This is deliberate — online-trust systems (e.g. Sigstore's
Fulcio/Rekor) cannot be reached in a Denied/Disrupted/Intermittent/Limited
(DDIL) environment.

The signature covers the package manifest and the SHA-256 of every file in the
package, so any tampering with the model, policy, or metadata is detected.

## Generate a keypair

```bash
temms keys generate --out-dir ./keys --name hub
# → keys/hub.private.pem   (secret; chmod 600 — lives on the Hub/signer only)
# → keys/hub.public.pem    (provision to every edge daemon)
# → Fingerprint: ed25519:XXXXXXXXXXXXXXXX
```

`temms keys fingerprint <keyfile>` prints the fingerprint of a private or public
key; both sides of a keypair share the same fingerprint, so it identifies the
signing identity in audit logs and evidence bundles.

## Sign and verify

```bash
# Hub: sign with the private key
temms package sign ./my-package --signing-key-file ./keys/hub.private.pem

# Edge: verify with the public key only
temms package validate ./my-package --require-signature \
  --signing-key-file ./keys/hub.public.pem
```

Key material may be supplied as PEM (as above), or a raw 32-byte Ed25519 key as
64-hex or base64 via `--signing-key`. The daemon reads its verification key from
`TEMMS_PACKAGE_SIGNING_KEY` / `TEMMS_PACKAGE_SIGNING_KEY_FILE` — provision the
**public** key there on edge nodes.

## Key rotation

1. `temms keys generate` a new keypair.
2. Provision the new public key to edge daemons (they can trust the new
   fingerprint alongside the old during a transition).
3. Sign new packages with the new private key.
4. Retire the old key once no in-field package depends on it.

## Backward compatibility (legacy HMAC)

Earlier packages used HMAC-SHA256 with a shared symmetric secret. Those remain
**verifiable** — `verify_package_signature` dispatches on the `algorithm` field
in `signature.json`, so a legacy package still validates with its shared key.
New packages should be signed with Ed25519; HMAC is retained only for
migration and carries the original caveat that anyone able to verify can also
forge. There is no silent upgrade: re-sign a package with an Ed25519 key to move
it onto asymmetric provenance.

## Where it's implemented

- `src/temms/core/signing.py` — `sign_package`, `verify_package_signature`,
  `generate_ed25519_keypair`, algorithm dispatch, key-type detection.
- `temms keys` CLI — keypair generation and fingerprints.
- Tests: `tests/unit/test_signing_ed25519.py`.
