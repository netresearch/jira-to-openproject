# Development Guide

This document contains information for developers working on the Jira to OpenProject migration tool.

## Development Environment Setup

1. Create a Python virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

2. Install the package in development mode:
```bash
pip install -e .
```

3. Install pre-commit hooks:
```bash
pre-commit install
```

4. Run the Docker container for development:
```bash
docker compose up -d
```

## Coding Standards

- PEP 8 style guide for Python code
- Type annotations for all function parameters and return values
- Comprehensive docstrings for all modules, classes, and functions
- Unit tests for all non-trivial functions
- Log actions appropriately using the configured logger

## Migration Design Principles

### Modular Components

The migration is divided into components that can be run independently:

- Users
- Custom Fields
- Companies (Tempo Accounts)
- Projects
- Link Types
- Issue Types
- Status Types
- Work Packages

### Two-Step Approach for Data Migration

Several migration components use a two-step approach:

1. **Basic Creation**: First, create the core entity with minimal required attributes
2. **Metadata Enhancement**: Then, update the entity with additional metadata, relationships, and custom fields

**Benefits of this approach:**

- **Better Error Handling**: If metadata update fails, the basic entity still exists
- **Cleaner Code Organization**: Separates the core creation logic from enhancement logic
- **Improved Performance**: Allows bulk creation of entities followed by selective updates
- **Flexibility**: Makes it possible to re-run just the metadata updates without recreating entities

**Example: Company Migration**

Companies are migrated in two distinct steps:
1. First, basic company projects are created with minimal attributes (name, identifier, description)
2. Then, a separate process enhances these projects with:
   - Customer contact information
   - Address details
   - Website links
   - Custom fields
   - Status settings

This allows the migration to recover gracefully if metadata update fails, as the basic company structure is already in place.

### Mappings

- All mappings between Jira and OpenProject IDs are stored in JSON files
- Mappings are used to establish relationships between entities in both systems
- Mappings can be reused between migration runs to avoid duplication

## Testing

- Unit tests are in the `tests/` directory
- Run tests with: `pytest`
- Integration tests should be run against test instances, not production

## Common Issues and Solutions

### Rails Console Interaction

When working with the Rails console:

- For large commands, use the `execute_via_file` method to avoid IO errors
- Use tmux sessions for persistent console connections
- Ensure proper error handling for all Rails interactions

### API Rate Limiting

- Both Jira and OpenProject APIs may have rate limits
- The migration tool includes rate limiting and pagination support
- For very large migrations, consider increasing timeouts in the configuration

## Contributing

1. Create a feature branch from `main`
2. Make your changes
3. Write or update tests for your changes
4. Ensure all tests pass
5. Submit a pull request
