.PHONY: help install dev-install sim-install test clean format lint build \
       docker-up docker-down docker-clean docker-build docker-logs \
       generate-models sim-weather sim-override test-e2e \
       init-local run-daemon

help:
	@echo "TEMMS - Tactical Edge Model Management System"
	@echo ""
	@echo "Available targets:"
	@echo "  install        - Install TEMMS in production mode"
	@echo "  dev-install    - Install TEMMS in development mode"
	@echo "  sim-install    - Install TEMMS with simulation dependencies"
	@echo "  test           - Run tests with pytest"
	@echo "  format         - Format code with black"
	@echo "  lint           - Lint code with ruff"
	@echo "  clean          - Remove build artifacts and caches"
	@echo "  build          - Build distribution packages"
	@echo ""
	@echo "Docker / Simulation:"
	@echo "  docker-up      - Start sim environment (MLflow + TEMMS daemon)"
	@echo "  docker-down    - Stop sim environment"
	@echo "  docker-clean   - Stop and remove all volumes (fresh state)"
	@echo "  docker-build   - Rebuild Docker images"
	@echo "  docker-logs    - Tail TEMMS daemon logs"
	@echo ""
	@echo "  generate-models - Generate real ONNX models for testing"
	@echo "  sim-weather    - Run weather change scenario"
	@echo "  sim-override   - Run operator override scenario"
	@echo "  test-e2e       - Run E2E tests (requires docker-up)"
	@echo ""
	@echo "Development:"
	@echo "  init-local     - Initialize TEMMS locally"
	@echo "  run-daemon     - Start daemon locally"

install:
	pip install -e .

dev-install:
	pip install -e ".[dev]"

sim-install:
	pip install -e ".[dev,sim]"

test:
	pytest

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
	@echo "Services starting:"
	@echo "  TEMMS Daemon: http://localhost:8080/ui/"
	@echo "  TEMMS API:    http://localhost:8080/v1/health"
	@echo "  MLflow UI:    http://localhost:5000"
	@echo ""
	@echo "Run 'make docker-logs' to see daemon output"

docker-down:
	docker compose down

docker-clean:
	docker compose down -v
	@echo "All volumes removed. Next docker-up will start fresh."

docker-logs:
	docker compose logs -f temms-daemon

generate-models:
	python scripts/generate_real_models.py

sim-weather:
	python scripts/sim_weather_scenario.py

sim-override:
	python scripts/sim_operator_override.py

test-e2e:
	pytest tests/integration/test_e2e_docker.py -v

# ---- Development shortcuts ----

init-local:
	temms init --config ./local.temms.yaml --data-dir ./local-data

run-daemon:
	temms daemon start --config ./local.temms.yaml
