.PHONY: help build up down restart logs shell test clean dev status ps exec install lint format type-check pre-commit install-irbrc start-rails attach-rails

# Default target
help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Development Environment:'
	@echo '  build          Build development containers'
	@echo '  up             Start the development stack (app + default services)'
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
	@echo 'Remote Ops:'
	@echo '  install-irbrc  Install contrib/openproject.irbrc into remote OpenProject container'
	@echo '  start-rails    Start local tmux session connected to remote Rails console'
	@echo '  attach-rails   Attach to the tmux Rails console session'
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

## mocks configured via compose test profile

build: ## Build development containers
	docker compose build

rebuild: ## Rebuild containers without cache
	docker compose build --no-cache

up dev: ## Start the development stack (app + default services)
	docker compose up -d


down dev-down: ## Stop all services
	docker compose down

restart: ## Restart development environment
	docker compose restart

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

install: ## Install/update Python dependencies in container (uv lock)
	docker compose exec app sh -lc 'uv sync --frozen --no-install-project'

install-test: ## Prepare the test container and its dependencies
	docker compose --profile test up -d test
	docker compose exec test sh -lc 'uv sync --frozen --no-install-project || uv sync --frozen'
	docker compose exec test sh -lc 'mkdir -p var/data var/results var/logs'

# Internal helper: prepare test container (deps + var dirs)
test-prep:
	docker compose --profile test up -d test
	docker compose exec test sh -lc 'uv sync --frozen --no-install-project || uv sync --frozen'
	docker compose exec test sh -lc 'mkdir -p var/data var/results var/logs'

# =============================================================================
# Testing and Quality
# =============================================================================

test: test-prep ## Run tests in dedicated test container (parallel execution)
	docker compose exec test sh -lc 'uv run python -m pytest -n auto'

test-verbose: test-prep ## Run tests with verbose output (parallel execution)
	docker compose exec test sh -lc 'uv run python -m pytest -v -n auto'

test-coverage: test-prep ## Run tests with coverage report (parallel execution)
	docker compose exec test sh -lc 'uv run python -m pytest -n auto --cov=src --cov-report=html --cov-report=term'

test-slow: test-prep ## Run slow tests only (integration/end-to-end)
	docker compose exec test sh -lc 'uv run python -m pytest -m "slow or integration or end_to_end" -n auto'

test-fast: test-prep ## Run fast tests only (unit tests)
	docker compose exec test sh -lc 'uv run python -m pytest -m "not slow and not integration and not end_to_end" -n auto'

test-live-ssh: test-prep ## Run tests with live SSH connections
	docker compose exec test sh -lc 'uv run python -m pytest --live-ssh -n auto'

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
# Remote Operations (OpenProject Host/Container)
# =============================================================================

install-irbrc: ## Install contrib/openproject.irbrc into remote OpenProject container
	@echo "Installing .irbrc into remote OpenProject container..."
	@set -a; [ -f .env ] && . ./.env; set +a; \
		if [ -z "$$J2O_OPENPROJECT_SERVER" ] || [ -z "$$J2O_OPENPROJECT_CONTAINER" ]; then \
			echo "Missing env: J2O_OPENPROJECT_SERVER and/or J2O_OPENPROJECT_CONTAINER"; \
			exit 1; \
		fi; \
		target=$${J2O_OPENPROJECT_USER:+$$J2O_OPENPROJECT_USER@}$$J2O_OPENPROJECT_SERVER; \
		scp contrib/openproject.irbrc "$$target:/tmp/.irbrc" && \
		ssh -t "$$target" "docker cp /tmp/.irbrc $$J2O_OPENPROJECT_CONTAINER:/app/.irbrc && rm -f /tmp/.irbrc && docker exec -u root -t $$J2O_OPENPROJECT_CONTAINER chmod 644 /app/.irbrc"

start-rails: install-irbrc ## Start local tmux session connected to the remote Rails console
	python scripts/start_rails_tmux.py $(if $(ATTACH),--attach,)

attach-rails: ## Attach to the tmux Rails console session
	tmux attach -t $${J2O_OPENPROJECT_TMUX_SESSION_NAME:-rails_console}

# =============================================================================
# Local Development (when not using containers)
# =============================================================================

local-install: ## Install dependencies locally (uv)
	uv sync --frozen

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

# Default service profiles (simplified: default and test)
PROFILES ?=

# Test options for flexible test execution
TEST_OPTS ?=
