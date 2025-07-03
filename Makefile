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

build: ## Build development containers
	docker compose build

rebuild: ## Rebuild containers without cache
	docker compose build --no-cache

up dev: ## Start development environment (app only)
	docker compose --profile dev up -d

dev-services: ## Start development with services (Redis, PostgreSQL)
	docker compose --profile dev --profile services up -d

dev-testing: ## Start development with testing services (mock APIs)
	docker compose --profile dev --profile testing up -d

dev-full: ## Start everything (app + services + testing)
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

# =============================================================================
# Testing and Quality
# =============================================================================

test: ## Run tests in container
	docker compose exec app python -m pytest

test-verbose: ## Run tests with verbose output
	docker compose exec app python -m pytest -v

test-coverage: ## Run tests with coverage report
	docker compose exec app python -m pytest --cov=src --cov-report=html --cov-report=term

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

local-test: ## Run tests locally
	python -m pytest

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
