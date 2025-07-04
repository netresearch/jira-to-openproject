# Development Environment Setup Guide

A comprehensive guide for setting up a modern, containerized development environment that balances consistency with developer productivity.

## 🎯 **Overview**

This development environment provides:
- **Fully containerized** development with VS Code Dev Container support
- **Volume-mounted source code** for instant code changes without rebuilds
- **Optional services** (databases, mock APIs, caching) via Docker profiles
- **Makefile-based CLI** for all common development tasks
- **Dual development modes**: containerized (primary) and local (fallback)
- **Zero host pollution** - only Docker required on the host machine

## 📁 **File Structure**

```
project/
├── .devcontainer/
│   └── devcontainer.json         # VS Code Dev Container configuration
├── .venv/                        # Local Python venv (optional, for quick debugging)
├── compose.yml                   # Docker Compose services (modern naming)
├── Dockerfile                    # Optimized development container
├── Makefile                      # CLI commands for all development tasks
├── requirements.txt              # Python dependencies
├── src/                          # Application source code
├── tests/                        # Test files
└── docs/
    └── DEVELOPMENT_ENVIRONMENT.md # This documentation
```

## 🚀 **Quick Start**

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) installed
- [VS Code](https://code.visualstudio.com/) with [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) (recommended)

### Option 1: VS Code Dev Container (Recommended)
```bash
# Clone the repository
git clone <repository-url>
cd <project-name>

# Open in VS Code
code .

# VS Code will prompt to "Reopen in Container" - click Yes
# Or manually: Ctrl+Shift+P → "Dev Containers: Reopen in Container"
```

### Option 2: Command Line Development
```bash
# Clone and enter directory
git clone <repository-url>
cd <project-name>

# Start development environment
make dev-setup

# Open shell in container
make shell
```

## ⚙️ **Environment Setup**

### Required Environment Variables

Some services require environment variables for proper operation:

### Step 1: Copy Environment Template
```bash
# Copy the template file
cp .env.example .env
```

### Step 2: Configure Required Variables
Edit `.env` and update the required values:

```bash
# DATABASE CONFIGURATION (Required for 'services' profile)
POSTGRES_DB=migration_test
POSTGRES_USER=testuser
POSTGRES_PASSWORD=your_secure_password_here  # ⚠️ UPDATE THIS
```

### Step 3: Environment Validation

The system automatically validates environment configuration:

```bash
# These commands check for required environment variables:
make dev-services   # Requires .env for PostgreSQL
make dev-full       # Requires .env for PostgreSQL

# These commands work without .env:
make dev           # App only (no databases)
make dev-testing   # Mock APIs only
```

### Troubleshooting Environment Issues

**Missing .env file error:**
```
❌ Missing .env file!
PostgreSQL service requires environment variables.
Quick fix:
  cp .env.example .env
  # Edit .env and set POSTGRES_PASSWORD
```

**Solution:** Follow steps 1-2 above to create and configure your `.env` file.

### Security Notes

- **Never commit `.env` to version control** - it contains sensitive data
- **Use `.env.example`** as a template for team sharing
- **Update passwords** from default values before use
- **Review `.gitignore`** to ensure `.env` is excluded

## 🔧 **Available Commands**

Run `make help` to see all available commands:

### Development Environment
- `make dev` - Start development environment (app only)
- `make dev-services` - Start with databases/caching services
- `make dev-testing` - Start with mock API services
- `make dev-full` - Start everything (app + services + testing)
- `make down` - Stop all services
- `make logs` - View logs from all services

### Development Tools
- `make shell` - Open bash shell in development container
- `make test` - Run tests in container
- `make lint` - Run linting (flake8, mypy)
- `make format` - Format code (black, isort)
- `make exec CMD="python --version"` - Execute arbitrary commands

### Local Development (Fallback)
- `make local-test` - Run tests locally
- `make local-lint` - Run linting locally
- `make local-format` - Format code locally

## 🏗️ **Architecture Details**

### Dockerfile Optimization

The `Dockerfile` is optimized for development with:
- **Layer caching**: Dependencies installed before source code copy
- **Non-root user**: Development runs as `vscode` user (UID 1000)
- **User-space installs**: Python packages installed to user directory
- **Minimal rebuilds**: Source code mounted as volume, not copied

```dockerfile
# Key optimizations in Dockerfile:
COPY --chown=vscode:vscode requirements.txt .    # Copy deps first
RUN pip install --user -r requirements.txt      # Install as non-root
COPY --chown=vscode:vscode . .                  # Copy code last
CMD ["sleep", "infinity"]                       # Keep container alive
```

### Docker Compose Profiles

Services are organized using Docker Compose profiles:

- **`dev`**: Core application container (always active)
- **`services`**: Optional persistence (Redis, PostgreSQL)
- **`testing`**: Mock APIs for testing (Prism mock servers)

#### Mock API Services

The `testing` profile includes mock API services for offline development:

- **Mock Jira API** (port 4010): Simulates Atlassian Jira REST API
- **Mock OpenProject API** (port 4011): Simulates OpenProject REST API

These services use [Stoplight Prism](https://stoplight.io/open-source/prism) to generate mock responses from OpenAPI specifications stored in `test-specs/`:

```
test-specs/
├── jira-openapi.yml          # Jira API specification
├── openproject-openapi.yml   # OpenProject API specification
└── README.md                 # Documentation
```

**Mock Service Usage:**
```bash
# Test mock Jira API
curl http://localhost:4010/rest/api/3/myself

# Test mock OpenProject API
curl http://localhost:4011/api/v3/users/me

# Start with mock services
make dev-testing
```

**Note:** These are minimal placeholder specifications designed to prevent Docker mount failures. For comprehensive testing, expand the OpenAPI specifications with additional endpoints and realistic responses.

```bash
# Start only the app
make dev

# Start app + databases
make dev-services

# Start app + mock APIs
make dev-testing

# Start everything
make dev-full
```

### Volume Mounting Strategy

```yaml
# In compose.yml
volumes:
  - .:/app                           # Source code (live editing)
  # SECURITY: Docker socket mount removed - see Security section below
```

This approach provides:
- **Instant feedback**: Code changes reflect immediately
- **No rebuilds**: Container doesn't need rebuilding for code changes
- **Isolated dependencies**: Container has its own Python environment

### Dev Container Integration

The `.devcontainer/devcontainer.json` configures VS Code to:
- Use the Docker Compose app service
- Forward relevant ports (8000, 4010, 4011)
- Install Python extensions automatically
- Configure Python interpreter path for user-installed packages

## 🛠️ **Development Workflows**

### Starting a Development Session

```bash
# Quick start (app only)
make dev

# Full environment with all services
make dev-full

# Check what's running
make status

# View logs
make logs
```

### Running Tests

```bash
# Basic test run
make test

# Verbose output
make test-verbose

# With coverage
make test-coverage

# Run specific test
make exec CMD="python -m pytest tests/test_specific.py"
```

### Code Quality

```bash
# Format code
make format

# Run linting
make lint

# Type checking
make type-check

# Run all quality checks
make check

# Pre-commit hooks
make pre-commit
```

### Working with Dependencies

```bash
# Add new dependency to requirements.txt, then:
make exec CMD="pip install --user package-name"

# Or rebuild container with new deps:
make rebuild
```

### Debugging

```bash
# Open interactive shell
make shell

# Execute one-off commands
make exec CMD="python -c 'import sys; print(sys.path)'"

# Check Python environment
make exec CMD="which python && python --version"

# View container logs
make logs-app
```

## 🔄 **Dual Development Modes**

### Containerized Development (Primary)

**Pros:**
- ✅ Consistent environment across all developers
- ✅ No host system pollution
- ✅ Isolated dependencies
- ✅ CI/CD environment parity
- ✅ Fast onboarding for new developers

**Cons:**
- ❌ Requires Docker knowledge
- ❌ Initial container build time
- ❌ Some IDEs may have limitations

**Use for:** Main development, team collaboration, CI/CD

### Local Development (Fallback)

**Pros:**
- ✅ Direct host access
- ✅ Native IDE integration
- ✅ No Docker overhead
- ✅ Familiar for traditional Python developers

**Cons:**
- ❌ Environment drift between developers
- ❌ Host system pollution
- ❌ "Works on my machine" issues
- ❌ Complex dependency management

**Use for:** Quick debugging, individual exploration, non-Docker environments

## 🎛️ **Configuration**

### Environment Variables

Create `.env` file in project root:
```bash
# API Configuration
JIRA_URL=https://your-jira.atlassian.net
JIRA_USERNAME=your-username
JIRA_API_TOKEN=your-token

OPENPROJECT_URL=https://your-openproject.com
OPENPROJECT_API_TOKEN=your-token

# Development Settings
LOG_LEVEL=DEBUG
PYTHONPATH=/app
```

### Port Configuration

Default ports (only exposed with appropriate profiles):
- `8000`: Primary application port (always available with --profile dev)
- `4010`: Mock Jira API (available with --profile dev)
- `4011`: Mock OpenProject API (available with --profile dev)
- `5432`: PostgreSQL (internal service - no external exposure)
- `6379`: Redis (internal service - no external exposure)

### Service Profiles & Security Architecture

This environment implements an **intelligent security-first architecture** using Docker Compose profiles:

#### Security by Default
```bash
# Default: ZERO exposed ports (maximum security)
docker compose up
# Result: No services exposed to host network

# Development: Minimal necessary exposure
docker compose --profile dev up
# Result: Only app (8000) and mock services (4010, 4011) exposed

# Full services: Internal services for integration testing
docker compose --profile dev --profile services up
# Result: App + mock services exposed, databases internal-only
```

#### Profile Strategy
```yaml
# No profile: Secure by default (no services)
# --profile dev: Development convenience (3 ports)
# --profile services: Add internal services (Redis, PostgreSQL)

app:
  # Always included in dev profile
  profiles: ["dev"]

mock-jira:
  # Only exposed with dev profile
  profiles: ["dev"]

redis:
  # Internal service - no external ports
  profiles: ["services"]
```

#### Benefits
- **🔒 Maximum Security**: Default operation exposes no attack surface
- **🛠️ Developer Convenience**: `--profile dev` provides necessary access
- **🏗️ Architectural Clarity**: Clear distinction between internal and external services
- **🎯 DevContainer Integration**: Perfect alignment with VS Code development workflow

## 🏭 **Production Considerations**

This setup is optimized for **development**. For production:

1. **Create separate Dockerfile.prod**:
   - Multi-stage build
   - Minimal base image
   - No development tools
   - Copy code instead of mounting

2. **Use production compose file**:
   - No volume mounts
   - Resource limits
   - Health checks
   - Restart policies

3. **Environment separation**:
   - Different environment variables
   - Secrets management
   - Database persistence

## 🔄 **Adapting for Other Projects**

### Python Projects

1. **Copy these files**:
   - `Dockerfile`
   - `compose.yml`
   - `.devcontainer/devcontainer.json`
   - `Makefile`
   - This documentation

2. **Customize**:
   - Update service names in `compose.yml`
   - Modify ports as needed
   - Adjust Python version in `Dockerfile`
   - Update project name in `devcontainer.json`

### Non-Python Projects

1. **Dockerfile**: Replace Python setup with your language's requirements
2. **Dependencies**: Update installation commands and paths
3. **Makefile**: Replace Python-specific commands (test, lint, format)
4. **Services**: Add language-specific services (databases, caches, etc.)

### Framework-Specific Adjustments

**Web Applications:**
- Add reverse proxy (nginx) service
- Configure hot reload for your framework
- Add database migration targets to Makefile

**APIs:**
- Include API documentation service (Swagger UI)
- Add integration test targets
- Configure mock external services

**Data Processing:**
- Add data storage services (S3, MinIO)
- Include job queue services (Redis, RabbitMQ)
- Add monitoring services (Prometheus, Grafana)

## 🐛 **Troubleshooting**

### Common Issues

**Container won't start:**
```bash
# Check logs
make logs

# Rebuild from scratch
make clean-all
make rebuild
```

**Permission issues:**
```bash
# Check user ID in container
make exec CMD="id"

# Fix file ownership (if needed)
sudo chown -R 1000:1000 .
```

**Port conflicts:**
```bash
# Check what's using ports
sudo netstat -tulpn | grep LISTEN

# Modify ports in compose.yml
```

**VS Code not detecting Python:**
```bash
# Check Python path
make exec CMD="which python"

# Update setting in devcontainer.json:
"python.defaultInterpreterPath": "/home/vscode/.local/bin/python"
```

### Performance Optimization

**Slow container builds:**
- Enable Docker BuildKit: `DOCKER_BUILDKIT=1`
- Use multi-stage builds for complex applications
- Leverage build cache: `docker compose build --pull`

**Slow file watching:**
- Exclude unnecessary directories in `.dockerignore`
- Use bind mounts instead of volumes for better performance on some systems

## 📚 **Best Practices**

### Development Workflow

1. **Start fresh daily**: `make dev-reset`
2. **Use profiles**: Start only needed services
3. **Clean regularly**: `make clean` to remove unused containers
4. **Test in container**: Ensure tests pass in the containerized environment

### Code Quality

1. **Format before commit**: `make format`
2. **Run all checks**: `make check`
3. **Use pre-commit hooks**: `make pre-commit`
4. **Test coverage**: `make test-coverage`

### Team Collaboration

1. **Document service requirements**: Update this README
2. **Pin dependency versions**: Use exact versions in `requirements.txt`
3. **Share environment variables**: Use `.env.example` template
4. **Update documentation**: Keep this guide current

## 🤝 **Contributing**

When modifying this development environment:

1. **Test changes**: Ensure both containerized and local development work
2. **Update documentation**: Modify this README for any changes
3. **Consider all platforms**: Test on different operating systems
4. **Backward compatibility**: Provide migration notes for breaking changes

## 🔒 **Security Best Practices**

This development environment follows security-first principles:

### Container Security

**Docker Socket Mount - REMOVED FOR SECURITY**
```yaml
# ❌ NEVER DO THIS (removed from our compose.yml):
volumes:
  - /var/run/docker.sock:/var/run/docker.sock
```

**Why this is dangerous:**
- Grants root-equivalent access to host Docker daemon
- Enables container escape attacks
- Bypasses all container isolation
- Unnecessary for SSH-based remote Docker operations

**Our secure alternative:**
- Use SSH-based Docker client (`src/clients/docker_client.py`)
- All Docker operations executed remotely via SSH
- No local Docker daemon access required

### Network Security

**Docker Container Binding vs Host Access Control**

Understanding the difference between internal container binding and host port mapping is crucial for secure development:

**Internal Container Binding (0.0.0.0)**
```yaml
# Inside containers, services bind to 0.0.0.0 to enable:
# - Cross-container communication (app → postgres)
# - Docker network connectivity
# - Service discovery by hostname
services:
  app:
    # Container process: python -m uvicorn --host=0.0.0.0 --port=8000
    # Binds to all interfaces INSIDE the container network
  postgres:
    # Container process: postgres listening on 0.0.0.0:5432
    # Available to other containers via hostname 'postgres:5432'
```

**Host Port Mapping (127.0.0.1 only)**
```yaml
# Docker port mapping controls HOST access:
services:
  app:
    ports:
      - "127.0.0.1:8000:8000"  # HOST:CONTAINER mapping
      # Host binding: Only 127.0.0.1 (localhost) can access
      # Container binding: Still 0.0.0.0 internally

  postgres:
    # NO ports section = No host access
    # Still accessible to containers via Docker network
```

**Security Model Diagram:**
```
┌─────────────────── HOST SYSTEM ──────────────────┐
│ Browser → 127.0.0.1:8000 ✓ (localhost only)     │
│ Network → 192.168.1.x:8000 ✗ (blocked)          │
│ ┌─────────── DOCKER NETWORK ──────────────┐     │
│ │  app:8000 ←→ postgres:5432             │     │
│ │  (0.0.0.0 binding enables              │     │
│ │   cross-container communication)        │     │
│ └─────────────────────────────────────────┘     │
└──────────────────────────────────────────────────┘
```

**Why this design is secure:**
- **Host Protection**: External networks cannot access development services
- **Container Functionality**: Internal 0.0.0.0 binding preserves Docker networking
- **Developer Access**: Browser and tools work normally via localhost
- **Service Isolation**: Databases remain internal-only, unexposed to host

**Port Mapping Strategy:**
```yaml
# ✅ SECURE: Intelligent exposure strategy
services:
  # Internal services - no host exposure
  redis:
    # Internal: redis:6379 (containers only)
    # Host: No access (secure)
  postgres:
    # Internal: postgres:5432 (containers only)
    # Host: No access (secure)

  # Development services - localhost-only host access
  app:
    ports:
      - "127.0.0.1:8000:8000"
    # Internal: app:8000 (+ 0.0.0.0 binding)
    # Host: localhost:8000 only

  mock-jira:
    ports:
      - "127.0.0.1:4010:4010"
    # Internal: mock-jira:4010 (+ 0.0.0.0 binding)
    # Host: localhost:4010 only
```

**Testing network security:**
```bash
# Verify intelligent port exposure strategy
ss -tulnp | grep -E "(8000|4010|4011)"

# Should show only 127.0.0.1:PORT bindings for exposed services
# Internal services (Redis, PostgreSQL) should not appear in results
```

**Further Reading:**
- [Docker Networking Overview](https://docs.docker.com/network/)
- [Docker Compose Port Mapping](https://docs.docker.com/compose/networking/#specify-custom-networks)
- [Container Network Security](https://docs.docker.com/engine/security/#docker-daemon-attack-surface)

**Cross-reference**: See `compose.yml` for complete implementation and `test-specs/README.md` for mock service security configuration.

### Development Security Guidelines

**Non-root User Execution**
```dockerfile
# All development runs as non-root user
USER vscode
RUN pip install --user -r requirements.txt
```

**Secrets Management**
```bash
# Use .env files (never commit secrets)
echo "SECRET_KEY=your-secret" >> .env
echo ".env" >> .gitignore

# For team sharing, use .env.example
cp .env .env.example
# Remove actual secrets from .env.example
```

**Resource Limits**

Implement container resource constraints to prevent resource exhaustion attacks and ensure stable development:

```yaml
# Example: Add to compose.yml services
services:
  app:
    deploy:
      resources:
        limits:
          memory: 1G        # Limit memory usage
          cpus: '1.0'       # Limit CPU usage
        reservations:
          memory: 256M      # Reserve minimum memory
          cpus: '0.25'      # Reserve minimum CPU

  postgres:
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: '0.5'
        reservations:
          memory: 128M
          cpus: '0.1'

  redis:
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: '0.25'
```

**Resource Guidelines:**
- **Development**: Generous limits to avoid disrupting workflows
- **CI/CD**: Stricter limits to ensure tests run in constrained environments
- **Production**: Resource limits based on capacity planning and monitoring

**Further Reading:**
- [Docker Compose Resource Constraints](https://docs.docker.com/compose/compose-file/deploy/#resources)
- [Container Resource Management](https://docs.docker.com/config/containers/resource_constraints/)

### Security Review Checklist

Before adding ANY Docker configuration:
- [ ] Is Docker socket mount really needed? (Usually NO)
- [ ] Are services running as non-root users?
- [ ] Are secrets excluded from version control?
- [ ] Are unnecessary ports closed?
- [ ] Are resource limits defined?
- [ ] Are volumes properly scoped?

### Secure Development Workflow

1. **Environment Isolation**: Development container isolates host system
2. **Volume Scoping**: Only mount necessary directories (source code)
3. **Network Segmentation**: Services use dedicated Docker network
4. **User Permissions**: All operations run as `vscode` user (UID 1000)
5. **Secret Management**: Use environment variables, never hardcode

### Alternative Solutions for Docker Access

If you ever need Docker daemon access (you probably don't):

**Option 1: Host Commands (Recommended)**
```bash
# Run Docker commands on host, not in container
make exec CMD="echo 'Use host Docker commands instead'"
```

**Option 2: Docker-in-Docker (Complex)**
```yaml
# Only if absolutely necessary
services:
  app:
    image: docker:dind
    privileged: true  # Security risk - avoid
```

**Option 3: Remote Docker Context (Advanced)**
```bash
# Use remote Docker context instead of socket mount
docker context create remote --docker "host=ssh://user@remote-host"
```

### Security Monitoring

**Regular Security Checks:**
```bash
# Audit container configuration
docker compose config | grep -E "(privileged|volumes|ports)"

# Check for security issues in Docker configuration
docker compose config --quiet && echo "✓ Compose configuration valid"

# Verify localhost-only bindings
ss -tulnp | grep -E "(8000|4010|4011)" | grep "127.0.0.1"

# Scan for vulnerabilities (requires setup)
# docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
#   aquasec/trivy image your-image-name

# Check for exposed secrets (requires git-secrets installation)
# git secrets --scan
```

## 📄 **License**

This development environment setup is provided as-is. Adapt it to your project's needs and licensing requirements.

---

**Last Updated:** {{ current_date }}
**Tested With:** Docker 24.x, VS Code 1.85+, Python 3.13+
