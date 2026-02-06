.PHONY: help install dev-install test clean format lint build

help:
	@echo "TEMMS - Tactical Edge Model Management System"
	@echo ""
	@echo "Available targets:"
	@echo "  install      - Install TEMMS in production mode"
	@echo "  dev-install  - Install TEMMS in development mode with dev dependencies"
	@echo "  test         - Run tests with pytest"
	@echo "  format       - Format code with black"
	@echo "  lint         - Lint code with ruff"
	@echo "  clean        - Remove build artifacts and caches"
	@echo "  build        - Build distribution packages"

install:
	pip install -e .

dev-install:
	pip install -e ".[dev]"

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

# Development shortcuts
init-local:
	temms init --config ./local.temms.yaml --data-dir ./local-data

run-daemon:
	temms daemon start --config ./local.temms.yaml
