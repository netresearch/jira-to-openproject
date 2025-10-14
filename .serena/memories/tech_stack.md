# Tech Stack and Dependencies

## Python Version
**Python 3.13+** (required, modern typing features)

## Core Dependencies

### HTTP & API
- **requests** (>=2.32.4): HTTP library for API calls
- **jira** (>=3.10.5): Jira API client

### Data Validation & Models
- **pydantic** (>=2.11.7): Data validation and settings management
- **jsonschema** (>=4.25.0): JSON schema validation

### Configuration & Environment
- **python-dotenv** (>=1.1.1): Environment variable management
- **pyyaml** (>=6.0.2): YAML configuration files

### CLI & Display
- **rich** (>=14.1.0): Console display and progress bars

### Database & Caching
- **redis** (>=6.4.0): Caching and idempotency tracking
- **sqlalchemy** (>=2.0.42): Database ORM for checkpointing

### Error Recovery & Resilience
- **tenacity** (>=9.1.2): Retry mechanisms with exponential backoff
- **pybreaker** (>=1.4.0): Circuit breaker pattern for fault tolerance
- **structlog** (>=25.4.0): Structured logging

### System Monitoring
- **psutil** (>=7.0.0): System resource monitoring

### Security
- **cryptography** (>=42.0.0): Cryptographic utilities

### Web Dashboard (Optional)
- **fastapi** (>=0.100.0): Web framework for dashboard
- **uvicorn** (>=0.23.0): ASGI server
- **websockets** (>=15.0.1): WebSocket support
- **jinja2** (>=3.1.6): Template engine
- **aiofiles** (>=24.1.0): Async file operations
- **aiohttp** (>=3.10.10): Async HTTP client

## Development Dependencies

### Testing
- **pytest** (>=8.4.1): Test framework
- **pytest-cov** (>=6.2.1): Coverage reporting
- **pytest-xdist** (>=3.8.0): Parallel test execution
- **pytest-asyncio** (>=0.23.8): Async test support

### Code Quality
- **pre-commit** (>=4.2.0): Git hook management
- **mypy** (>=1.17.1): Static type checking
- **ruff** (>=0.12.7): Fast Python linter and formatter
- **types-PyYAML** (>=6.0.12.20240808): Type stubs for PyYAML
- **types-aiofiles** (>=23.2.0.4): Type stubs for aiofiles
- **types-requests** (>=2.32.0.20241016): Type stubs for requests

## Package Manager
**uv**: Fast Python package installer and resolver
- Commands: `uv sync --frozen` (install), `uv run` (execute)

## External Tools Required

### Docker & Compose
- **Docker**: Container runtime for development/testing environment
- **Docker Compose**: Multi-container orchestration

### SSH & Remote Access
- **SSH client**: For remote OpenProject server access
- **tmux**: Terminal multiplexer for Rails console sessions

### Build Tools
- **make**: Build automation (see Makefile for targets)

## Configuration Precedence
1. Environment variables (highest priority)
2. `.env.local` (developer-specific overrides)
3. `.env` (project defaults)
4. `config/config.yaml` (structured configuration)
5. Code defaults (lowest priority)

## Key Environment Variables
- `J2O_JIRA_URL`, `J2O_JIRA_USERNAME`, `J2O_JIRA_API_TOKEN`
- `J2O_OPENPROJECT_URL`, `J2O_OPENPROJECT_API_KEY`
- `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_USER`, `J2O_OPENPROJECT_CONTAINER`
- `J2O_BATCH_SIZE`, `J2O_LOG_LEVEL`, `J2O_SSL_VERIFY`
- `POSTGRES_PASSWORD` (for Docker services)
