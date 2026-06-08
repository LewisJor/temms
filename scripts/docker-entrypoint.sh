#!/bin/bash
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "==============================="
echo "  TEMMS Sim Environment"
echo "==============================="
echo ""

# 1. Generate real ONNX models if needed
# Check if models are still the old 295-byte dummies
DAYLIGHT_MODEL="/app/examples/package-example/models/yolov8n-daylight.onnx"
if [ ! -f "$DAYLIGHT_MODEL" ] || [ "$(wc -c < "$DAYLIGHT_MODEL")" -lt 1000 ]; then
    echo "[1/5] Generating real ONNX models..."
    python /app/scripts/generate_real_models.py --output-dir /app/examples/package-example
else
    echo "[1/5] Real ONNX models already present, skipping generation."
fi

# 2. Initialize TEMMS
echo ""
echo "[2/5] Initializing TEMMS..."
temms init --config /etc/temms/temms.yaml --data-dir /var/lib/temms 2>&1 || true

# 3. Import example model package (skip hash verify since we just generated them)
echo ""
echo "[3/5] Importing model package..."
temms import /app/examples/package-example/ --config /etc/temms/temms.yaml --no-verify --allow-unsigned-package 2>&1 || echo "  (package may already be imported)"

# 4. Create vision slot
echo ""
echo "[4/5] Creating vision slot..."
temms slot create vision \
    --description "Primary vision model" \
    --required \
    --default yolov8-daylight \
    --config /etc/temms/temms.yaml 2>&1 || echo "  (slot may already exist)"

# 5. Copy policies to config directory
echo ""
echo "[5/5] Loading policies..."
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
