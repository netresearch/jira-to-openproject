.PHONY: help build up down restart logs shell test clean dev dev-down dev-services dev-testing dev-full status ps exec install lint format type-check pre-commit

# Default target
help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Development Environment:'
	@echo '  build          Build development containers'
	@echo '  dev            Start development environment (app only)'
	@echo '  dev-services   Start with optional services (Redis, PostgreSQL)'
	@echo '  dev-testing    Start with testing services (mock APIs)'
	@echo '  dev-full       Start everything (app + services + testing)'
	@echo '  up             Alias for dev'
	@echo '  down           Stop all services'
	@echo '  restart        Restart development environment'
	@echo '  logs           Show logs from all services'
	@echo '  status         Show running containers'
	@echo '  ps             Alias for status'
	@echo ''
	@echo 'Development Tools:'
	@echo '  shell          Open shell in development container'
	@echo '  exec           Execute command in dev container (e.g., make exec CMD="python --version")'
	@echo '  install        Install/update Python dependencies'
	@echo '  test           Run tests in container'
	@echo '  lint           Run linting (flake8, mypy)'
	@echo '  format         Format code (black, isort)'
	@echo '  type-check     Run type checking'
	@echo '  pre-commit     Run pre-commit hooks'
	@echo ''
	@echo 'Maintenance:'
	@echo '  clean          Clean up containers, volumes, and cache'
	@echo '  rebuild        Clean build (no cache)'
	@echo ''
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# =============================================================================
# Build and Environment Management
# =============================================================================

check-env: ## Validate required environment variables for services
	@echo "Checking environment configuration..."
	@if [ ! -f .env ]; then \
		echo "❌ Missing .env file!"; \
		echo ""; \
		echo "PostgreSQL service requires environment variables."; \
		echo "Quick fix:"; \
		echo "  cp .env.example .env"; \
		echo "  # Edit .env and set POSTGRES_PASSWORD"; \
		echo ""; \
		echo "For help: cat .env.example"; \
		exit 1; \
	fi
	@if [ -f .env ] && [ -z "$$(grep '^POSTGRES_PASSWORD=' .env | cut -d'=' -f2)" ]; then \
		echo "❌ POSTGRES_PASSWORD not set in .env file!"; \
		echo ""; \
		echo "Please edit .env and set a secure password:"; \
		echo "  POSTGRES_PASSWORD=your_secure_password_here"; \
		exit 1; \
	fi
	@echo "✅ Environment configuration OK"

ensure-test-specs: ## Ensure test-specs directory exists for mock services
	@echo "Checking test-specs directory..."
	@if [ ! -d test-specs ]; then \
		echo "❌ Missing test-specs directory!"; \
		echo ""; \
		echo "Mock services require API specifications."; \
		echo "This directory should be created automatically."; \
		echo "If you see this error, please report it as a bug."; \
		exit 1; \
	fi
	@if [ ! -f test-specs/jira-openapi.yml ] || [ ! -f test-specs/openproject-openapi.yml ]; then \
		echo "❌ Missing OpenAPI specification files!"; \
		echo ""; \
		echo "Required files:"; \
		echo "  test-specs/jira-openapi.yml"; \
		echo "  test-specs/openproject-openapi.yml"; \
		echo ""; \
		echo "These files should be created automatically."; \
		echo "If you see this error, please report it as a bug."; \
		exit 1; \
	fi
	@echo "✅ Test specifications OK"

build: ## Build development containers
	docker compose build

rebuild: ## Rebuild containers without cache
	docker compose build --no-cache

up dev: ## Start development environment (app only)
	docker compose --profile dev up -d

dev-services: check-env ## Start development with services (Redis, PostgreSQL)
	docker compose --profile dev --profile services up -d

dev-testing: ensure-test-specs ## Start development with testing services (mock APIs)
	docker compose --profile dev --profile testing up -d

dev-full: check-env ensure-test-specs ## Start everything (app + services + testing)
	docker compose --profile dev --profile services --profile testing up -d

down dev-down: ## Stop all services
	docker compose down

restart: ## Restart development environment
	docker compose --profile dev restart

stop: ## Stop services without removing containers
	docker compose stop

# =============================================================================
# Monitoring and Logs
# =============================================================================

logs: ## Show logs from all services
	docker compose logs -f

logs-app: ## Show logs from app service only
	docker compose logs -f app

status ps: ## Show running containers
	docker compose ps

# =============================================================================
# Development Tools
# =============================================================================

shell: ## Open bash shell in development container
	docker compose exec app bash

exec: ## Execute command in dev container (use CMD="command here")
	@if [ -z "$(CMD)" ]; then \
		echo "Usage: make exec CMD=\"your command here\""; \
		echo "Example: make exec CMD=\"python --version\""; \
		exit 1; \
	fi
	docker compose exec app $(CMD)

install: ## Install/update Python dependencies in container
	docker compose exec app pip install --user -r requirements.txt

install-test: ## Install/update Python dependencies in test container
	docker compose --profile test up -d test

# =============================================================================
# Testing and Quality
# =============================================================================

test: ## Run tests in dedicated test container (parallel execution)
	docker compose --profile test up -d test
	docker compose exec test python -m pytest -n auto

test-verbose: ## Run tests with verbose output (parallel execution)
	docker compose --profile test up -d test
	docker compose exec test python -m pytest -v -n auto

test-coverage: ## Run tests with coverage report (parallel execution)
	docker compose --profile test up -d test
	docker compose exec test python -m pytest -n auto --cov=src --cov-report=html --cov-report=term

test-slow: ## Run slow tests only (integration/end-to-end)
	docker compose --profile test up -d test
	docker compose exec test python -m pytest -m "slow or integration or end_to_end" -n auto

test-fast: ## Run fast tests only (unit tests)
	docker compose --profile test up -d test
	docker compose exec test python -m pytest -m "not slow and not integration and not end_to_end" -n auto

test-live-ssh: ## Run tests with live SSH connections
	docker compose --profile test up -d test
	docker compose exec test python -m pytest --live-ssh -n auto

lint: ## Run linting (flake8, mypy)
	docker compose exec app flake8 src tests
	docker compose exec app mypy src

format: ## Format code (black, isort)
	docker compose exec app black src tests
	docker compose exec app isort src tests

type-check: ## Run type checking
	docker compose exec app mypy src

pre-commit: ## Run pre-commit hooks
	docker compose exec app pre-commit run --all-files

# =============================================================================
# Local Development (when not using containers)
# =============================================================================

local-install: ## Install dependencies locally (for local venv usage)
	pip install -r requirements.txt

# Fast development path - bypasses Docker overhead
dev-test: ## Run tests locally for fast development feedback (recommended for daily use)
	python -m pytest -n auto $(TEST_OPTS)

dev-test-fast: ## Run fast tests locally for immediate feedback (unit tests only)
	python -m pytest -m "not slow and not integration and not end_to_end" -n auto $(TEST_OPTS)

dev-test-slow: ## Run slow tests locally (integration/end-to-end)
	python -m pytest -m "slow or integration or end_to_end" -n auto $(TEST_OPTS)

dev-test-live-ssh: ## Run tests locally with live SSH connections
	python -m pytest --live-ssh -n auto $(TEST_OPTS)

# Legacy local targets (kept for compatibility)
local-test: ## Run tests locally (parallel execution)
	python -m pytest -n auto

local-test-fast: ## Run fast tests locally (unit tests only)
	python -m pytest -m "not slow and not integration and not end_to_end" -n auto

local-test-slow: ## Run slow tests locally (integration/end-to-end)
	python -m pytest -m "slow or integration or end_to_end" -n auto

local-test-live-ssh: ## Run tests locally with live SSH connections
	python -m pytest --live-ssh -n auto

local-lint: ## Run linting locally
	flake8 src tests
	mypy src

local-format: ## Format code locally
	black src tests
	isort src tests

# =============================================================================
# Maintenance and Cleanup
# =============================================================================

clean: ## Clean up containers, volumes, and cache
	docker compose down -v --remove-orphans
	docker compose --profile test down -v --remove-orphans
	docker system prune -f
	docker volume prune -f

clean-all: ## Nuclear option: remove everything including images
	docker compose down -v --remove-orphans --rmi all
	docker system prune -af
	docker volume prune -f

# =============================================================================
# Docker Compose Shortcuts
# =============================================================================

pull: ## Pull latest images
	docker compose pull

config: ## Show docker compose configuration
	docker compose config

# =============================================================================
# Development Workflow Helpers
# =============================================================================

check: lint test ## Run all checks (lint + test)

dev-setup: build dev ## Complete development setup (build + start)

dev-reset: clean build dev ## Reset development environment

# Quality gates
ci: format lint test ## Run CI pipeline locally

# =============================================================================
# Variables and Configuration
# =============================================================================

# Allow passing additional docker compose arguments
COMPOSE_ARGS ?=

# Default service profiles
PROFILES ?= dev

# Test options for flexible test execution
TEST_OPTS ?=
