.PHONY: help install dev-install sim-install test clean format lint build \
       docker-up docker-down docker-clean docker-build docker-logs \
       generate-models sim-weather sim-override sim-visual sim-headless test-e2e \
       init-local run-daemon

# ==============================================================================
#  TEMMS — Makefile
#  "make help" shows all targets. "make sim-visual" is the one you want.
# ==============================================================================

help:
	@echo ""
	@echo "  ╔════════════════════════════════════════════════════════════════╗"
	@echo "  ║  TEMMS — Tactical Edge Model Management System               ║"
	@echo "  ╚════════════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "  Getting Started:"
	@echo "    make dev-install       Install TEMMS + dev dependencies"
	@echo "    make test              Run all tests (268 tests)"
	@echo ""
	@echo "  Docker Sim Environment:"
	@echo "    make docker-up         Start everything (MLflow + TEMMS daemon)"
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

# ---- Docker / Simulation ----

docker-build:
	docker compose build

docker-up:
	docker compose up --build -d
	@echo ""
	@echo "  ┌────────────────────────────────────────┐"
	@echo "  │  Services starting...                   │"
	@echo "  │                                         │"
	@echo "  │  TEMMS UI:    http://localhost:8080/ui/  │"
	@echo "  │  TEMMS API:   http://localhost:8080/v1/  │"
	@echo "  │  MLflow UI:   http://localhost:5000      │"
	@echo "  │  API Docs:    http://localhost:8080/docs │"
	@echo "  │                                         │"
	@echo "  │  Next: make sim-headless                │"
	@echo "  └────────────────────────────────────────┘"

docker-down:
	docker compose down

docker-clean:
	docker compose down -v
	@echo "All volumes removed. Next docker-up will start fresh."

docker-logs:
	docker compose logs -f temms-daemon

# ---- Simulation runners ----

generate-models:
	python scripts/generate_real_models.py

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
