.PHONY: help install dev-install sim-install test clean format lint build \
       docker-up docker-down docker-clean docker-build docker-build-runtime docker-buildx docker-logs docker-product-smoke \
       generate-models product-demo sim-weather sim-override sim-visual sim-headless test-e2e \
       mvp-smoke mvp-acceptance soak soak-short docker-acceptance docker-acceptance-up \
       docker-acceptance-down init-local run-daemon ui-install ui-build ui-ci ui-dev ui-smoke ui-typecheck

# ==============================================================================
#  TEMMS — Makefile
#  "make help" shows all targets. "make sim-visual" is the one you want.
# ==============================================================================

export MLFLOW_HOST_PORT ?= 5001

help:
	@echo ""
	@echo "  ╔════════════════════════════════════════════════════════════════╗"
	@echo "  ║  TEMMS — Tactical Edge Model Management System               ║"
	@echo "  ╚════════════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "  Getting Started:"
	@echo "    make dev-install       Install TEMMS + dev dependencies"
	@echo "    make product-demo      Run the canonical signed/adaptive/evidence demo"
	@echo "    make test              Run all tests"
	@echo "    make mvp-smoke         Run signed Hub Lite air-gap and online rollout smoke tests"
	@echo "    make mvp-acceptance    Run multi-edge MVP acceptance flow"
	@echo ""
	@echo "  Docker Sim Environment:"
	@echo "    make docker-up         Start everything (MLflow + TEMMS daemon)"
	@echo "    make docker-build-runtime Build local default runtime target image"
	@echo "    make docker-buildx     Build multi-arch agent image with buildx bake"
	@echo "    make docker-acceptance     Run containerized Hub + two edge acceptance"
	@echo "    make docker-acceptance-up  Start Hub + two edge agent containers"
	@echo "    make docker-product-smoke  Verify live Mission Package Workbench routes"
	@echo "    make docker-down       Stop all containers"
	@echo "    make docker-clean      Nuke volumes, start fresh"
	@echo "    make docker-logs       Tail daemon logs"
	@echo ""
	@echo "  Visual Simulation (the cool part):"
	@echo "    make sim-visual        Run fog scenario with live GUI window"
	@echo "    make sim-headless      Run fog scenario in text mode (Docker/CI)"
	@echo "    make sim-weather       Run API-only weather scenario"
	@echo "    make sim-override      Run API-only operator override scenario"
	@echo ""
	@echo "  Code Quality:"
	@echo "    make format            Format with black"
	@echo "    make lint              Lint with ruff + mypy"
	@echo "    make ui-install        Install React Hub UI dependencies"
	@echo "    make ui-build          Build the React Hub UI into src/temms/ui/static"
	@echo "    make ui-ci             Typecheck, build, and smoke the React Hub UI"
	@echo "    make ui-smoke          Verify the built Runtime workbench UI contract"
	@echo "    make ui-dev            Run the React Hub UI dev server"
	@echo "    make clean             Remove build artifacts"
	@echo ""

# ---- Install targets ----

install:
	pip install -e .

dev-install:
	pip install -e ".[dev]"

sim-install:
	pip install -e ".[dev,sim]"

sim-visual-install:
	pip install -e ".[dev,sim-visual]"

# ---- Test targets ----

test:
	pytest

test-e2e:
	pytest tests/integration/test_e2e_docker.py -v

mvp-smoke:
	uv run pytest tests/integration/test_canonical_product_loop.py tests/integration/test_hub_lite_mvp_flow.py tests/integration/test_hub_lite_online_sync.py tests/integration/test_mvp_multi_vm_acceptance.py -q

mvp-acceptance:
	uv run pytest tests/integration/test_mvp_multi_vm_acceptance.py -q

# ---- Soak & chaos reliability harness (#13) ----

soak-short:
	uv run pytest tests/integration/test_soak_smoke.py -q

soak:
	uv run python scripts/soak.py --duration $${SOAK_DURATION:-120} \
		--report docs/reliability-report.json --markdown docs/reliability-report.md

crash-soak:
	uv run python scripts/crash_soak.py --iterations $${CRASH_ITERATIONS:-40} \
		--report docs/crash-atomicity-report.json

crash-soak-selftest:
	@echo "Atomicity deliberately broken — this run is EXPECTED to fail:"
	@TEMMS_CRASH_SOAK_UNSAFE_WRITES=1 uv run python scripts/crash_soak.py \
		--iterations 8 || echo "  ^ expected FAIL: the harness detects torn writes"

test-sim:
	pytest tests/test_sim_weather.py tests/test_sim_scenarios.py -v

# ---- Code quality ----

format:
	black src/ tests/

lint:
	ruff check src/ tests/
	mypy src/

clean:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +

build:
	python -m build

# ---- React Hub UI ----

ui-install:
	cd ui && npm install

ui-build:
	cd ui && npm run build

ui-typecheck:
	cd ui && npm run typecheck

ui-ci:
	npm run ui:ci

ui-smoke:
	cd ui && npm run smoke:workbench

ui-dev:
	cd ui && npm run dev

# ---- Docker / Simulation ----

docker-build:
	docker compose build

docker-build-runtime:
	docker build --platform linux/amd64 --build-arg TEMMS_EXTRAS=inference -t temms/agent:inference-amd64 .

docker-buildx:
	docker buildx bake -f docker-bake.hcl

docker-up:
	docker compose up --build -d
	@echo ""
	@echo "  ┌────────────────────────────────────────┐"
	@echo "  │  Services starting...                   │"
	@echo "  │                                         │"
	@echo "  │  TEMMS Hub:   http://localhost:8080/ui/hub │"
	@echo "  │  TEMMS API:   http://localhost:8080/v1/  │"
	@printf "  │  MLflow UI:   http://localhost:%-9s │\n" "$(MLFLOW_HOST_PORT)"
	@echo "  │  API Docs:    http://localhost:8080/docs │"
	@echo "  │                                         │"
	@echo "  │  Next: make sim-headless                │"
	@echo "  └────────────────────────────────────────┘"

docker-down:
	docker compose down

docker-acceptance-up:
	docker compose -f deploy/docker-compose.acceptance.yml up --build -d
	@echo ""
	@echo "  Acceptance agents:"
	@echo "    Hub:         http://localhost:$${TEMMS_ACCEPTANCE_HUB_PORT:-18080}"
	@echo "    Online edge: http://localhost:$${TEMMS_ACCEPTANCE_ONLINE_PORT:-18081}"
	@echo "    Airgap edge: http://localhost:$${TEMMS_ACCEPTANCE_AIRGAP_PORT:-18082}"
	@echo ""
	@echo "  Run deploy/multi-vm-acceptance.sh connected-lab with package paths under /acceptance-packages."

docker-acceptance-down:
	docker compose -f deploy/docker-compose.acceptance.yml down

docker-acceptance:
	deploy/docker-acceptance-run.sh

docker-clean:
	docker compose down -v
	@echo "All volumes removed. Next docker-up will start fresh."

docker-logs:
	docker compose logs -f temms-daemon

docker-product-smoke:
	python scripts/mission_package_smoke.py --hub-url http://localhost:8080

# ---- Simulation runners ----

generate-models:
	python scripts/generate_real_models.py

product-demo:
	uv run python scripts/canonical_product_demo.py

# Visual sim with live GUI window (needs: pip install -e ".[sim-visual]")
sim-visual:
	python -m temms.sim.runner --scenario fog_rollout

sim-visual-night:
	python -m temms.sim.runner --scenario day_night_cycle

sim-visual-rain:
	python -m temms.sim.runner --scenario rainstorm

sim-visual-stress:
	python -m temms.sim.runner --scenario combined_stress

# Headless sim (text output, works in Docker/CI)
sim-headless:
	python -m temms.sim.runner --scenario fog_rollout --headless

# API-only simulation scripts (no video, just condition injection)
sim-weather:
	python scripts/sim_weather_scenario.py

sim-override:
	python scripts/sim_operator_override.py

# ---- Development shortcuts ----

init-local:
	temms init --config ./local.temms.yaml --data-dir ./local-data

run-daemon:
	temms daemon start --config ./local.temms.yaml

# ---- Week 1 runtime ops ----

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f temms-daemon

health:
	curl -fsS http://localhost:8080/v1/health

metrics:
	curl -fsS http://localhost:8080/metrics

deploy:
	curl -fsS -X POST http://localhost:8080/v1/control/deploy -H "Content-Type: application/json" -d "{}"

state:
	cat /var/lib/temms/deployment_state.json || true

offline:
	curl -fsS -X POST http://localhost:8080/v1/control/offline

online:
	curl -fsS -X POST http://localhost:8080/v1/control/online

sync:
	curl -fsS -X POST http://localhost:8080/v1/control/sync
