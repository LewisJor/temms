# Canonical JSON Hashing (`temms-canonical-json/v1`)

Every TEMMS proof/package/evidence digest is a SHA-256 over a **canonical JSON
serialization**. For a digest to be verifiable by *any* client — the React Hub,
the CLI, and a future native (Rust/C++) port — every implementation must produce
byte-identical canonical bytes. This document is that contract.

Reference implementation: `core.mission_package.canonical_json_hash`
(`_canonicalize_hash_numbers` + `json.dumps`). It is the single source of truth
used by the daemon, Hub, CLI, package signing, and the decision chain.

## The rules

Given a JSON value, produce the digest as follows.

1. **Number canonicalization.** An integer-valued number MUST be encoded as an
   integer, whether the source language typed it as an integer or a float.
   So `12`, `12.0`, and `4096.0` all serialize as `12` / `4096`. This is the key
   rule: JSON has no int/float distinction and JavaScript has only IEEE-754
   doubles, so `12.0` collapses to `12` after a Python → JSON → JS → JSON round
   trip. Without this rule the recomputed digest would differ per language.
   - **Booleans are not numbers.** `true`/`false` are never coerced.
   - **Non-integer numbers** (e.g. `0.72`) are serialized in their shortest
     round-trippable decimal form (as `json.dumps`/`JSON.stringify` produce for
     the values TEMMS uses — SLO thresholds, confidence, scores). Avoid relying
     on exotic floats (very large magnitudes, `1e300`, sub-ULP differences); the
     provenance payloads are small, bounded numbers.
2. **Object keys** are sorted ascending by Unicode code point, and every level
   of nesting is sorted.
3. **No insignificant whitespace.** The item separator is `,` and the key/value
   separator is `:` (i.e. `separators=(",", ":")`).
4. **Strings** are standard JSON strings, `null` for JSON null, `true`/`false`
   for booleans. Non-ASCII characters are emitted as **raw UTF-8**, not `\uXXXX`
   escapes (i.e. `ensure_ascii=False`, matching `JSON.stringify`). This keeps the
   bytes identical across languages for strings like `"Détecter"` or `"🛰"`.
5. **Encoding + hash.** UTF-8 encode the canonical text, then SHA-256; the digest
   is the lowercase hex string.

Non-JSON-native values (e.g. a datetime object) are stringified (`default=str`)
before hashing — but payloads SHOULD be JSON-native; treat a stringified value
as a bug in the caller, not a feature.

## Worked example

```json
{"b": 2.0, "a": "x", "c": [1.0, 0.5, true]}
```

canonicalizes to the bytes

```
{"a":"x","b":2,"c":[1,0.5,true]}
```

and the digest is `SHA-256(those bytes)`.

## Conformance vectors

`tests/vectors/canonical_json_vectors.json` pins input → expected digest for the
cases that matter (integer-valued floats, genuine floats, nested structures,
booleans-are-not-numbers, unicode, key ordering). A reimplementation is
conformant iff it reproduces every expected digest.

- Regenerate after an intentional contract change:
  `python scripts/gen_canonical_vectors.py`
- `tests/unit/test_canonical_vectors.py` verifies the reference implementation
  against the committed vectors, so an accidental change to the canonicalization
  fails CI.

## Relationship to RFC 8785 (JCS)

These rules are aligned with the JSON Canonicalization Scheme (RFC 8785) for the
value space TEMMS uses, and the integer-valued-float rule matches JCS number
handling. TEMMS/v1 does **not** claim full RFC 8785 conformance (it does not
implement JCS's complete number-formatting or string-escaping algorithm). A
native port that needs to interoperate beyond the committed vectors should
implement RFC 8785 and re-pin the vectors.
