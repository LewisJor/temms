#!/bin/bash
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "==============================="
echo "  TEMMS Sim Environment"
echo "==============================="
echo ""

# Sign with an Ed25519 demo key: real asymmetric, offline-verifiable provenance
# for packages and the decision chain. Generated once into the data volume; the
# public key is what an auditor uses to verify. Falls back to the legacy HMAC
# key only if key generation is unavailable.
TEMMS_DEMO_KEY_DIR="${TEMMS_DEMO_KEY_DIR:-/var/lib/temms/keys}"
if [ -z "${TEMMS_PACKAGE_SIGNING_KEY:-}" ] && [ -z "${TEMMS_PACKAGE_SIGNING_KEY_FILE:-}" ]; then
    mkdir -p "${TEMMS_DEMO_KEY_DIR}"
    if [ ! -f "${TEMMS_DEMO_KEY_DIR}/demo.private.pem" ]; then
        temms keys generate --out-dir "${TEMMS_DEMO_KEY_DIR}" --name demo >/dev/null 2>&1 \
            && echo "  Generated Ed25519 demo signing keypair in ${TEMMS_DEMO_KEY_DIR}"
    fi
    if [ -f "${TEMMS_DEMO_KEY_DIR}/demo.private.pem" ]; then
        export TEMMS_PACKAGE_SIGNING_KEY="$(cat "${TEMMS_DEMO_KEY_DIR}/demo.private.pem")"
        echo "  Signing with Ed25519 (public key: ${TEMMS_DEMO_KEY_DIR}/demo.public.pem)"
    fi
fi
export TEMMS_PACKAGE_SIGNING_KEY="${TEMMS_PACKAGE_SIGNING_KEY:-temms-local-demo-signing-key}"
export TEMMS_ROLLOUT_REQUIRE_SIGNATURE="${TEMMS_ROLLOUT_REQUIRE_SIGNATURE:-true}"
export TEMMS_DEMO_SEED_HUB="${TEMMS_DEMO_SEED_HUB:-1}"

# 1. Generate real ONNX models if needed
# Check if models are still the old 295-byte dummies
DAYLIGHT_MODEL="/app/examples/package-example/models/yolov8n-daylight.onnx"
if [ ! -f "$DAYLIGHT_MODEL" ] || [ "$(wc -c < "$DAYLIGHT_MODEL")" -lt 1000 ]; then
    echo "[1/6] Generating real ONNX models..."
    python /app/scripts/generate_real_models.py --output-dir /app/examples/package-example
else
    echo "[1/6] Real ONNX models already present, skipping generation."
fi

# 2. Initialize TEMMS
echo ""
echo "[2/6] Initializing TEMMS..."
temms init --config /etc/temms/temms.yaml --data-dir /var/lib/temms 2>&1 || true

# 3. Import example model package (skip hash verify since we just generated them)
echo ""
echo "[3/6] Importing model package..."
temms import /app/examples/package-example/ --config /etc/temms/temms.yaml --no-verify --allow-unsigned-package 2>&1 || echo "  (package may already be imported)"

# 4. Create vision slot
echo ""
echo "[4/6] Creating vision slot..."
slot_create_log="/tmp/temms-slot-create.log"
if temms slot create vision \
    --description "Primary vision model" \
    --required \
    --default yolov8-daylight \
    --config /etc/temms/temms.yaml >"$slot_create_log" 2>&1; then
    cat "$slot_create_log"
else
    if grep -q "UNIQUE constraint failed: slots.name" "$slot_create_log"; then
        echo "  Slot vision already exists."
    else
        cat "$slot_create_log"
        exit 1
    fi
fi

# 5. Seed Hub Lite with signed, released demo inventory
echo ""
if [ "${TEMMS_DEMO_SEED_HUB:-1}" != "0" ]; then
    echo "[5/6] Seeding signed Hub demo inventory..."
    python /app/scripts/seed_docker_hub_demo.py \
        --package-source /app/examples/package-example \
        --data-dir /var/lib/temms 2>&1 || echo "  (Hub demo inventory may already be seeded)"
else
    echo "[5/6] Hub demo inventory seeding disabled."
fi

# 6. Copy policies to config directory
echo ""
echo "[6/6] Loading policies..."
cp /app/examples/policies/*.yaml /etc/temms/policies/ 2>/dev/null || true
echo "  Copied policies to /etc/temms/policies/"

# List what's available
echo ""
echo "==============================="
echo "  System Ready"
echo "==============================="
echo ""
echo "  API:     http://0.0.0.0:8080/v1/health"
echo "  UI:      http://0.0.0.0:8080/ui/"
echo "  MLflow:  http://mlflow-server:5000"
echo ""

# Start daemon in foreground
echo "Starting TEMMS daemon..."
exec temms daemon start --foreground --host 0.0.0.0 --port 8080 --config /etc/temms/temms.yaml
