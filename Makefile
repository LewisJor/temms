.PHONY: help install dev-install sim-install test clean format lint build \
       docker-up docker-down docker-clean docker-build docker-build-runtime docker-buildx docker-logs \
       generate-models product-demo sim-headless test-e2e \
       mvp-smoke mvp-acceptance soak soak-short docker-acceptance docker-acceptance-up \
       docker-acceptance-down init-local run-daemon

# ==============================================================================
#  TEMMS — Makefile
#  "make help" shows all targets.
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
	@echo "    make docker-down       Stop all containers"
	@echo "    make docker-clean      Nuke volumes, start fresh"
	@echo "    make docker-logs       Tail daemon logs"
	@echo ""
	@echo "  Simulation:"
	@echo "    make sim-headless      Step a DDIL scenario against the daemon"
	@echo ""
	@echo "  Code Quality:"
	@echo "    make format            Format with black"
	@echo "    make lint              Lint with ruff + mypy"
	@echo "    make clean             Remove build artifacts"
	@echo ""

# ---- Install targets ----

install:
	pip install -e .

dev-install:
	pip install -e ".[dev]"

sim-install:
	pip install -e ".[dev,sim]"

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

# Falsification: with atomicity deliberately broken the harness MUST fail.
# A passing run here means the harness has stopped detecting corruption, which
# is worse than a failing soak — so that outcome fails this target.
crash-soak-selftest:
	@echo "Atomicity deliberately broken — this run is EXPECTED to fail:"
	@if TEMMS_CRASH_SOAK_UNSAFE_WRITES=1 uv run python scripts/crash_soak.py --iterations 8; then \
		echo ""; \
		echo "ERROR: the harness PASSED despite torn writes — it is no longer detecting corruption."; \
		exit 1; \
	else \
		echo ""; \
		echo "OK: the harness detected torn writes (the failure above is the expected result)."; \
	fi

test-sim:
	pytest tests/test_sim_weather.py tests/test_sim_scenarios.py -v

# ---- Code quality ----

format:
	black src/ tests/

# Mirrors the CI gates exactly, so a green `make lint` means a green CI lint.
lint:
	uv run ruff check src/ tests/ scripts/
	uv run python scripts/check_no_duplicate_defs.py
	mypy src/

clean:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +

build:
	python -m build

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

# arm64 (Pi-class) edge acceptance — native on Apple Silicon, no hardware needed.
docker-acceptance-arm64:
	uv run python scripts/arm64_acceptance.py

docker-acceptance-arm64-down:
	docker compose -f deploy/docker-compose.acceptance.yml \
		-f deploy/docker-compose.acceptance.arm64.yml down -v

docker-acceptance:
	deploy/docker-acceptance-run.sh

docker-clean:
	docker compose down -v
	@echo "All volumes removed. Next docker-up will start fresh."

docker-logs:
	docker compose logs -f temms-daemon

# ---- Simulation runners ----

generate-models:
	python scripts/generate_real_models.py

product-demo:
	uv run python scripts/canonical_product_demo.py

sim-headless:
	python -m temms.sim.runner --scenario fog_rollout

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
