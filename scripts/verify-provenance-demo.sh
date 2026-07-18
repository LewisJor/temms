#!/usr/bin/env bash
# Demo: prove the tamper-evident decision chain offline (issue #27).
#
# Extracts the daemon's Ed25519 public key, exports the evidence bundle, verifies
# the signed decision chain, then tampers with a decision and shows detection.
# Requires the local Docker stack running (make docker-up).
set -euo pipefail

HUB_URL="${HUB_URL:-http://localhost:8080}"
DAEMON_CONTAINER="${DAEMON_CONTAINER:-temms-daemon}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Fetching the edge's public key and evidence"
docker cp "${DAEMON_CONTAINER}:/var/lib/temms/keys/demo.public.pem" "$WORK/public.pem"
curl -sf "${HUB_URL}/v1/evidence" -o "$WORK/evidence.json"

echo
echo "==> Verify the decision chain offline (public key only)"
uv run temms evidence --input "$WORK/evidence.json" \
  --verify-chain --public-key "$WORK/public.pem"

echo
echo "==> Tamper with one recorded decision, then re-verify"
python3 - "$WORK/evidence.json" "$WORK/tampered.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
bundle = json.load(open(src))
entries = bundle.get("decision_chain", {}).get("entries") or []
if not entries:
    raise SystemExit("No decision chain entries to tamper with")
entries[0]["to_model"] = "malicious-model"
json.dump(bundle, open(dst, "w"))
print(f"   (edited decision entry 0 -> to_model=malicious-model)")
PY

set +e
uv run temms evidence --input "$WORK/tampered.json" \
  --verify-chain --public-key "$WORK/public.pem"
status=$?
set -e
echo
if [ "$status" -eq 2 ]; then
  echo "✅ Tampering detected (exit $status) — the evidence chain is tamper-evident."
else
  echo "❌ Expected tampering to be detected (exit 2), got $status"
  exit 1
fi
