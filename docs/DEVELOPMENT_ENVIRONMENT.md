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
  - /var/run/docker.sock:/var/run/docker.sock  # Docker access
```

This approach provides:
- **Instant feedback**: Code changes reflect immediately
- **No rebuilds**: Container doesn't need rebuilding for code changes
- **Isolated dependencies**: Container has its own Python environment

### Dev Container Integration

The `.devcontainer/devcontainer.json` configures VS Code to:
- Use the Docker Compose app service
- Forward relevant ports (8000, 5000, 4010, 4011)
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

Default ports (configurable in `compose.yml`):
- `8000, 5000`: Application ports
- `4010`: Mock Jira API
- `4011`: Mock OpenProject API
- `5432`: PostgreSQL
- `6379`: Redis

### Service Profiles

Customize which services start by default by modifying profiles in `compose.yml`:

```yaml
# Always start with app
profiles: ["dev"]

# Include in dev-services
profiles: ["services"]

# Include in dev-testing
profiles: ["testing"]
```

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

## 📄 **License**

This development environment setup is provided as-is. Adapt it to your project's needs and licensing requirements.

---

**Last Updated:** {{ current_date }}
**Tested With:** Docker 24.x, VS Code 1.85+, Python 3.13+
