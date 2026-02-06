#!/usr/bin/env bash
#
# TEMMS Phase 1 Manual Test Script
# Run this to validate all Phase 1 functionality before starting Phase 2.
#
# Usage: bash scripts/test_phase1.sh
#
# Don't use set -e; we track failures with PASS/FAIL counters

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

PASS=0
FAIL=0
TEST_DIR=$(mktemp -d /tmp/temms-phase1-test-XXXX)
CFG="$TEST_DIR/config/temms.yaml"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

cleanup() {
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT

step() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}STEP: $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

check() {
    local result=$?
    if [ $result -eq 0 ]; then
        echo -e "  ${GREEN}PASS${NC}: $1"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}FAIL${NC}: $1"
        FAIL=$((FAIL + 1))
    fi
    return 0  # Never propagate failure
}

expect_output() {
    local output="$1"
    local pattern="$2"
    local desc="$3"
    if echo "$output" | grep -q "$pattern"; then
        PASS=$((PASS + 1))
        echo -e "  ${GREEN}PASS${NC}: $desc"
    else
        FAIL=$((FAIL + 1))
        echo -e "  ${RED}FAIL${NC}: $desc (pattern '$pattern' not found)"
    fi
}

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           TEMMS Phase 1 Validation Test Suite               ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Test dir: $TEST_DIR"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# ─────────────────────────────────────────────────────────────
step "1. Run pytest suite"
# ─────────────────────────────────────────────────────────────
OUTPUT=$(pytest tests/ -v --tb=short 2>&1)
echo "$OUTPUT" | tail -3
echo "$OUTPUT" | grep -q "passed"
check "All unit and integration tests pass"

# ─────────────────────────────────────────────────────────────
step "2. Initialize TEMMS"
# ─────────────────────────────────────────────────────────────
OUTPUT=$(temms init --data-dir "$TEST_DIR/data" --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "Created configuration" "temms init creates config file"
test -f "$CFG"
check "Config file exists on disk"
test -d "$TEST_DIR/data/models"
check "Models directory created"
test -d "$TEST_DIR/data/cache"
check "Cache directory created"

# Verify config round-trips correctly
OUTPUT=$(temms status --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Cached models: 0" "Config loads back without error"

# ─────────────────────────────────────────────────────────────
step "3. Import example package"
# ─────────────────────────────────────────────────────────────
OUTPUT=$(temms import "$PROJECT_DIR/examples/package-example/" --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "Package imported successfully" "Package import succeeds"
expect_output "$OUTPUT" "Models imported: 3" "All 3 models imported"
expect_output "$OUTPUT" "yolov8-daylight" "yolov8-daylight model present"
expect_output "$OUTPUT" "yolov8-lowlight" "yolov8-lowlight model present"
expect_output "$OUTPUT" "mobilenet-tiny" "mobilenet-tiny model present"
expect_output "$OUTPUT" "Policies imported: 1" "Policy imported"

# Verify status reflects the import
OUTPUT=$(temms status --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Cached models: 3" "Status shows 3 cached models"
expect_output "$OUTPUT" "Imported packages: 1" "Status shows 1 package"

# Verify model files on disk
test -d "$TEST_DIR/data/models/model-yolov8-daylight-001"
check "yolov8-daylight stored on disk"
test -d "$TEST_DIR/data/models/model-yolov8-lowlight-001"
check "yolov8-lowlight stored on disk"
test -d "$TEST_DIR/data/models/model-mobilenet-tiny-001"
check "mobilenet-tiny stored on disk"

# ─────────────────────────────────────────────────────────────
step "4. Create slots"
# ─────────────────────────────────────────────────────────────
OUTPUT=$(temms slot create vision -d "Primary perception" --required \
    --default yolov8-daylight \
    --candidates "yolov8-daylight,yolov8-lowlight,mobilenet-tiny" \
    --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "Created slot: vision" "Vision slot created"

OUTPUT=$(temms slot create targeting -d "Target tracking" \
    --default yolov8-daylight \
    --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Created slot: targeting" "Targeting slot created"

OUTPUT=$(temms slot list --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "vision" "Vision slot appears in list"
expect_output "$OUTPUT" "targeting" "Targeting slot appears in list"
expect_output "$OUTPUT" "stopped" "Slots start in stopped state"

# ─────────────────────────────────────────────────────────────
step "5. Activate models in slots"
# ─────────────────────────────────────────────────────────────
OUTPUT=$(temms slot set vision yolov8-daylight --reason "initial setup" --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "Activated yolov8-daylight" "Model activated in vision slot"

OUTPUT=$(temms slot status vision --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "State: running" "Slot is now running"
expect_output "$OUTPUT" "yolov8-daylight" "Correct model name in candidates"

# Switch to a different model
OUTPUT=$(temms slot set vision mobilenet-tiny --reason "test switch" --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Activated mobilenet-tiny" "Model switch succeeds"

# Verify decision log shows both activations
OUTPUT=$(temms slot decisions --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "initial" "First activation logged"
expect_output "$OUTPUT" "test" "Second activation logged"

# ─────────────────────────────────────────────────────────────
step "6. Load and list policies"
# ─────────────────────────────────────────────────────────────
OUTPUT=$(temms policy load "$PROJECT_DIR/examples/policies/thermal-adaptive.yaml" --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "Loaded policy: thermal-adaptive-vision" "Thermal policy loaded"
expect_output "$OUTPUT" "Slot: vision" "Policy targets vision slot"
expect_output "$OUTPUT" "Rules: 2" "Policy has 2 rules"

OUTPUT=$(temms policy load "$PROJECT_DIR/examples/policies/weather-adaptive.yaml" --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Loaded policy: weather-adaptive-vision" "Weather policy loaded"

OUTPUT=$(temms policy load "$PROJECT_DIR/examples/policies/battery-adaptive.yaml" --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Loaded policy: battery-adaptive-multislot" "Battery policy loaded"

OUTPUT=$(temms policy list --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "thermal-adaptive" "Thermal policy in list"
expect_output "$OUTPUT" "weather-adaptive" "Weather policy in list"
expect_output "$OUTPUT" "battery-adaptive" "Battery policy in list"

# ─────────────────────────────────────────────────────────────
step "7. Inject and manage conditions"
# ─────────────────────────────────────────────────────────────
# Set CPU temperature (operator override)
OUTPUT=$(temms condition set platform.compute.cpu_temp_c 80 --source operator --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "Condition set" "CPU temp condition set"

# Set weather visibility
OUTPUT=$(temms condition set environmental.atmospheric.visibility_m 50 --source operator --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Condition set" "Visibility condition set"

# Set battery (using sensor priority)
OUTPUT=$(temms condition set platform.power.battery_pct 15 --source sensor --priority 100 --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Condition set" "Battery condition set"

# List all conditions
OUTPUT=$(temms condition list --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "cpu_temp_c" "CPU temp in condition list"
expect_output "$OUTPUT" "visibi" "Visibility in condition list"
expect_output "$OUTPUT" "battery_pct" "Battery in condition list"

# Get specific condition
OUTPUT=$(temms condition get platform.compute.cpu_temp_c --config "$CFG" 2>&1)
expect_output "$OUTPUT" "80" "CPU temp value is 80"

# View nested snapshot
OUTPUT=$(temms condition snapshot --config "$CFG" 2>&1)
echo "$OUTPUT"
expect_output "$OUTPUT" "platform" "Snapshot has platform category"
expect_output "$OUTPUT" "environmental" "Snapshot has environmental category"

# ─────────────────────────────────────────────────────────────
step "8. Test condition priority (operator > sensor)"
# ─────────────────────────────────────────────────────────────
# Sensor (priority 100) should NOT override operator (priority 1000)
OUTPUT=$(temms condition set platform.compute.cpu_temp_c 55 --source sensor --priority 100 --config "$CFG" 2>&1)
OUTPUT=$(temms condition get platform.compute.cpu_temp_c --config "$CFG" 2>&1)
expect_output "$OUTPUT" "80" "Sensor cannot override operator (priority respected)"

# Clear overrides
OUTPUT=$(temms condition clear-overrides --config "$CFG" 2>&1)
expect_output "$OUTPUT" "Cleared" "Operator overrides cleared"

# Now sensor value should stick
OUTPUT=$(temms condition set platform.compute.cpu_temp_c 55 --source sensor --priority 100 --config "$CFG" 2>&1)
OUTPUT=$(temms condition get platform.compute.cpu_temp_c --config "$CFG" 2>&1)
expect_output "$OUTPUT" "55" "After clearing overrides, sensor value sticks"

# ─────────────────────────────────────────────────────────────
step "9. Verify SQLite database"
# ─────────────────────────────────────────────────────────────
DB="$TEST_DIR/data/temms.db"
test -f "$DB"
check "Database file exists"

TABLES=$(sqlite3 "$DB" ".tables" 2>&1)
echo "  Tables: $TABLES"
echo "$TABLES" | grep -q "cached_models"
check "cached_models table exists"
echo "$TABLES" | grep -q "packages"
check "packages table exists"
echo "$TABLES" | grep -q "slots"
check "slots table exists"
echo "$TABLES" | grep -q "conditions"
check "conditions table exists"
echo "$TABLES" | grep -q "slot_decisions"
check "slot_decisions table exists"
echo "$TABLES" | grep -q "condition_history"
check "condition_history table exists"

MODEL_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM cached_models;" 2>&1)
test "$MODEL_COUNT" -eq 3
check "3 models in database"

DECISION_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM slot_decisions;" 2>&1)
test "$DECISION_COUNT" -ge 2
check "At least 2 decisions logged"

# ─────────────────────────────────────────────────────────────
step "10. Verify checksum validation"
# ─────────────────────────────────────────────────────────────
# Create a package with bad checksums
BAD_PKG="$TEST_DIR/bad-package"
mkdir -p "$BAD_PKG/models" "$BAD_PKG/policies"
echo "corrupted data" > "$BAD_PKG/models/bad-model.onnx"
cat > "$BAD_PKG/manifest.json" << 'MANIFEST'
{
  "schema_version": "v1",
  "package_id": "pkg-bad-001",
  "name": "bad-package",
  "version": "1.0.0",
  "created_at": "2024-01-01T00:00:00Z",
  "models": [
    {
      "id": "model-bad-001",
      "name": "bad-model",
      "version": "1.0.0",
      "format": "onnx",
      "filename": "bad-model.onnx",
      "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "size_bytes": 15
    }
  ],
  "policies": []
}
MANIFEST

OUTPUT=$(temms import "$BAD_PKG" --config "$CFG" 2>&1) || true
expect_output "$OUTPUT" "Hash mismatch" "Bad checksum correctly rejected"

# ─────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
if [ $FAIL -eq 0 ]; then
    echo -e "${BOLD}║  ${GREEN}ALL TESTS PASSED: $PASS passed, $FAIL failed${NC}${BOLD}                       ║${NC}"
    echo -e "${BOLD}║  ${GREEN}Phase 1 is ready for Phase 2!${NC}${BOLD}                                ║${NC}"
else
    echo -e "${BOLD}║  ${RED}TESTS COMPLETE: $PASS passed, $FAIL failed${NC}${BOLD}                        ║${NC}"
    echo -e "${BOLD}║  ${RED}Fix failures before starting Phase 2${NC}${BOLD}                        ║${NC}"
fi
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"

exit $FAIL
