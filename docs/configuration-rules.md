# Configuration Rules - Quick Reference

## One-Liner Rule
**Configuration uses direnv (`.envrc`) as single source of truth for environment variables, with Pydantic v2 validation, CLI tools for management, and clear precedence: direnv → .env.test (test mode) → YAML → defaults.**

## Configuration Precedence (Highest to Lowest)
1. **Environment Variables** (from direnv `.envrc`)
2. **Test Environment** (`.env.test` overrides when in test mode)
3. **YAML Configuration** (`config/config.yaml` and environment-specific files)
4. **Pydantic Defaults** (fallback values in Settings model)

## Key Files
- **`.envrc`** - Single source of truth for all environment variables (direnv)
- **`.env.test`** - Test-specific overrides (only loaded in test mode)
- **`.env.example`** - Template for new setups
- **`config/config.yaml`** - Base YAML configuration
- **`config/environments/*.yaml`** - Environment-specific overrides
- **`config/schemas/settings.py`** - Pydantic Settings model with validation
- **`config/loader.py`** - Configuration loading logic
- **`config/cli.py`** - CLI tools for configuration management

## CLI Tools
```bash
python -m config.cli validate          # Validate configuration
python -m config.cli test-connections  # Test service connections
python -m config.cli show              # Show configuration summary
python -m config.cli show --secrets    # Show configuration with secrets
python -m config.cli export --format json  # Export configuration as JSON
python -m config.cli export --format yaml  # Export configuration as YAML
python -m config.cli create-envrc      # Create .envrc template for direnv
```

## Environment Variables (J2O_* prefix)
- **J2O_JIRA_*** - Jira configuration (URL, username, API token, etc.)
- **J2O_OPENPROJECT_*** - OpenProject configuration (URL, API token, SSH settings, etc.)
- **J2O_*** - Migration settings (batch size, SSL verify, log level, etc.)
- **J2O_TEST_*** - Testing configuration (test mode, mock mode, etc.)
- **POSTGRES_*** - Database configuration (password, database, user)

## Setup Process
1. **Enable direnv**: `direnv allow`
2. **Update credentials**: Edit `.envrc` with real Jira/OpenProject credentials
3. **Validate**: `python -m config.cli validate`
4. **Test connections**: `python -m config.cli test-connections`

## Best Practices
- **Single source of truth**: Use `.envrc` for all environment variables
- **Type safety**: All configuration validated by Pydantic
- **Environment separation**: Use `.env.test` only for test-specific overrides
- **No secrets in VCS**: All `.env*` files in `.gitignore`
- **Validation first**: Always validate configuration before running migrations
- **CLI tools**: Use provided CLI tools for configuration management 