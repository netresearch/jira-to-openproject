# Configuration System Rule

## One-Liner Rule
**Configuration System**: Uses Pydantic v2 + pydantic-settings with J2O_* environment variables, YAML config files, and .env files in precedence order (CLI > env vars > .envrc > .env > YAML), with schema validation, CLI tools, and direnv integration.

## Quick Reference

### Configuration Sources (Precedence)
1. Command-line arguments
2. Environment variables (J2O_* prefix)
3. .envrc (direnv - project-specific environment)
4. .env (base configuration)
5. config/config.yaml (YAML settings)
6. config/environments/{environment}.yaml (environment-specific overrides)

### Key Files
- `.env` - Base environment variables
- `.envrc` - direnv project-specific variables
- `.env.example` - Template with documentation
- `config/config.yaml` - Structured YAML configuration
- `config/schemas/settings.py` - Pydantic settings models
- `config/loader.py` - Configuration loading logic
- `config/cli.py` - CLI tools for configuration management

### CLI Tools
```bash
python -m config.cli validate          # Validate configuration
python -m config.cli test-connections  # Test service connections
python -m config.cli show              # Show configuration summary
python -m config.cli export --format json  # Export configuration
python -m config.cli create-envrc      # Create direnv template
```

### Programmatic Usage
```python
from config import load_settings, get_config_loader

# Load settings
settings = load_settings()

# Access configuration
print(f"Jira URL: {settings.jira_url}")
print(f"Batch Size: {settings.batch_size}")
```

### Environment Variables
- `J2O_JIRA_*` - Jira configuration
- `J2O_OPENPROJECT_*` - OpenProject configuration
- `J2O_*` - Migration settings
- `J2O_TEST_*` - Testing configuration
- `POSTGRES_*` - Database configuration

### Validation
- Type safety with Pydantic v2
- Automatic validation of URLs, tokens, paths
- Clear error messages with actionable feedback
- IDE support with autocomplete and type hints 