# Jira to OpenProject Migration Tool

A robust, modular migration toolset for transferring project management data from Jira Server 9.11 to OpenProject 15. Built with Python 3.13, this tool handles users, projects, work packages, custom fields, statuses, workflows, and attachments.

## Features

**Migration Components:**
- **Customer Migration:** Tempo Customers → Top-level OpenProject projects
- **Account Migration:** Tempo Accounts → Custom field values on projects  
- **Project Migration:** Jira Projects → Sub-projects under customer hierarchy
- **Work Package Migration:** Issues → Work packages with full metadata
- **User Migration:** Jira users → OpenProject users with role mapping
- **Custom Field Migration:** Field definitions and values
- **Status & Workflow Migration:** Status creation and workflow configuration guidance
- **Attachment Migration:** Issue attachments → Work package files
- **Time Log Migration:** Tempo worklogs → OpenProject time entries

**Key Capabilities:**
- **Batch Processing:** Configurable batch sizes for large datasets
- **Progress Tracking:** Real-time progress monitoring with detailed logging
- **Error Recovery:** Comprehensive error handling with retry mechanisms
- **Security:** Input validation and injection attack prevention
- **Flexible Deployment:** Local execution or Docker containerization
- **Testing Infrastructure:** Comprehensive test suite with multiple test types

## Quick Start

### Installation

```bash
# Clone and setup
git clone <repository-url>
cd jira-to-openproject-migration
python -m venv .venv
source .venv/bin/activate  # or activate.sh
pip install -e .

# Configure environment
cp .env.example .env
# Edit .env with your Jira and OpenProject credentials
```

### Development Environment

```bash
# Full development setup with services
make dev-full

# Run tests
make test

# Code quality checks
make lint
make type-check
```

### Migration Execution

```bash
# Run specific migration components
python src/main.py migrate --components users,projects,workpackages

# Run with custom configuration
python src/main.py migrate --config custom_config.yaml --components all

# Dry run mode
python src/main.py migrate --dry-run --components users
```

## Architecture

### System Overview

The migration tool uses a **layered client architecture** for reliable remote operations:

```
Local Migration Tool
    ↓ (SSH Connection)
Remote OpenProject Server  
    ↓ (Docker Commands)
OpenProject Container
    ↓ (Rails Console)
OpenProject Application
```

### Client Components

- **OpenProjectClient**: High-level orchestration and API
- **SSHClient**: Foundation layer for remote connections
- **DockerClient**: Container operations via SSH
- **RailsConsoleClient**: Rails console interaction via tmux

### Migration Flow

1. **Extract**: Data extracted from Jira via REST API
2. **Transform**: Data mapping and validation
3. **Load**: Data insertion via OpenProject Rails console
4. **Verify**: Validation and error reporting

## Configuration

### Environment Variables

Critical settings in `.env`:

```bash
# Jira Configuration
J2O_JIRA_URL=https://your-jira-server
J2O_JIRA_USERNAME=your-username
J2O_JIRA_API_TOKEN=your-api-token

# OpenProject Configuration  
J2O_OPENPROJECT_URL=https://your-openproject
J2O_OPENPROJECT_API_KEY=your-api-key

# SSH Connection (for remote OpenProject)
SSH_HOST=openproject-server
SSH_USER=username
SSH_KEY_FILE=/path/to/ssh/key

# Docker Configuration
DOCKER_CONTAINER_NAME=openproject-web-1
TMUX_SESSION_NAME=rails_console
```

### YAML Configuration

Structured settings in `config/config.yaml`:

```yaml
migration:
  batch_size: 100
  parallel_workers: 4
  enable_dry_run: false
  skip_validation: false
  
logging:
  level: INFO
  format: detailed
  file: migration.log
```

## Documentation

### Core Documentation

- **[Developer Guide](docs/DEVELOPER_GUIDE.md)** - Development standards, testing, and compliance
- **[System Architecture](docs/ARCHITECTURE.md)** - Client architecture and component design
- **[Security Documentation](docs/SECURITY.md)** - Security measures and vulnerability prevention
- **[Configuration Guide](docs/configuration.md)** - Detailed configuration options
- **[Workflow & Status Guide](docs/WORKFLOW_STATUS_GUIDE.md)** - Status migration and workflow setup

### Quick Reference

**Development:**
```bash
# Quick unit tests (~30s)
python scripts/test_helper.py quick

# Full test suite (~5-10min)
python scripts/test_helper.py full

# Type checking
mypy src/
```

**Migration:**
```bash
# Status and workflow migration
python src/main.py migrate --components status,workflow --force

# Full migration with progress tracking
python src/main.py migrate --components all --verbose
```

## Development

### Prerequisites

- **Python 3.13+**
- **Docker & Docker Compose** (for development environment)
- **SSH access** to OpenProject server (for production migration)
- **tmux** (for Rails console interaction)

### Development Standards

- **Exception-based error handling** (no return codes)
- **Optimistic execution** (validate in exception handlers)
- **Modern Python typing** (built-in types, pipe operators)
- **Comprehensive testing** (unit, functional, integration)
- **Security-first** (input validation, injection prevention)

### Testing

```bash
# Test organization
tests/
├── unit/          # Fast, isolated component tests
├── functional/    # Component interaction tests  
├── integration/   # External service tests
├── end_to_end/    # Complete workflow tests
└── utils/         # Shared testing utilities
```

## Security

### Input Validation

All user inputs (especially Jira keys) are validated:
- Character whitelisting (`A-Z`, `0-9`, `-` only)
- Length limits (max 100 characters)
- Control character blocking

### Injection Prevention

- Ruby script generation uses safe `inspect` method
- No direct string interpolation in generated code
- Comprehensive validation before execution

## Performance

### Optimization Features

- **Batch processing** with configurable sizes
- **Connection pooling** for SSH connections
- **Adaptive polling** for console operations
- **Parallel processing** for independent operations
- **Progress tracking** with minimal overhead

### Monitoring

- Real-time progress reporting
- Detailed logging with configurable levels
- Error aggregation and reporting
- Performance metrics collection

## Troubleshooting

### Common Issues

**Connection Problems:**
- Verify SSH key permissions (`chmod 600`)
- Check OpenProject container status
- Validate tmux session configuration

**Migration Errors:**
- Review logs in `var/logs/`
- Check data validation reports
- Verify OpenProject permissions

**Performance Issues:**
- Adjust batch sizes in configuration
- Monitor system resources
- Check network connectivity

### Debug Commands

```bash
# Test connections
python scripts/test_connections.py

# Validate configuration
python scripts/validate_config.py

# Debug specific migration
python src/main.py migrate --debug --components users --limit 10
```

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

1. **Follow development standards** outlined in [Developer Guide](docs/DEVELOPER_GUIDE.md)
2. **Write comprehensive tests** for all changes
3. **Validate security** for any user input processing
4. **Update documentation** as needed
5. **Run full test suite** before submitting changes

For architecture changes, see [System Architecture](docs/ARCHITECTURE.md) for design patterns and component responsibilities.
