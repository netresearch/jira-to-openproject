# Migration Progress Tracker

## Current Status

### Plan Progress
- [x] Define acceptance criteria
- [x] Clarify specifications
- [x] Identify impediments
- [x] Identify what to import and in which order
- [x] Select programming language (Python)
- [x] Create data mapping strategy
- [x] Develop migration scripts
- [x] Set up test environment
- [ ] Validate migrated data
- [ ] Perform user acceptance testing
- [x] Create rollback strategy
- [ ] Schedule production migration

## Migration Components

### Available Components
The following components can be migrated:

- `users`: Users from Jira to OpenProject
- `custom_fields`: Custom field mapping and Rails script generation
- `companies`: Company information as top-level projects
- `accounts`: Tempo accounts
- `projects`: Jira projects to OpenProject
- `link_types`: Issue link types to work package relation types
- `issue_types`: Work package type mapping and Rails script generation
- `work_packages`: Jira issues to OpenProject work packages

### Running Migrations

#### Full Migration (Dry Run)
```bash
python run_migration.py --dry-run
```

#### Specific Components
```bash
python run_migration.py --dry-run --components users projects custom_fields
```

#### Production Migration
```bash
python run_migration.py
```

> **Note**: Migration behavior is affected by configuration settings such as batch size and rate limits. For configuration details, see [Configuration Guide](./docs/configuration.md).

### Manual Steps Required

#### Custom Fields Import
1. Generate Ruby script:
   ```bash
   python -m src.migrations.custom_field_migration --generate-ruby
   ```

2. Run via Rails console:
   ```bash
   cd /opt/openproject
   bundle exec rails console
   load '/path/to/custom_fields_import.rb'
   ```

#### Work Package Types Import
1. Generate Ruby script:
   ```bash
   python -m src.migrations.issue_type_migration --generate-ruby
   ```

2. Run via Rails console:
   ```bash
   cd /opt/openproject
   bundle exec rails console
   load '/path/to/work_package_types_import.rb'
   ```

3. Update mapping file with new IDs

### Implementation Notes

#### Data Mapping Strategy
- Custom field types conversion
- Workflow state normalization
- User identity mapping via AD/LDAP
- Company/account structure mapping

#### Testing Strategy
- Use dry-run mode first
- Test components individually
- Verify mappings manually

#### Workflow Migration
- OpenProject status availability
- Basic status lifecycle preservation
- Transition mapping

#### Work Package Structure
- Epics → OpenProject Epics
- Issues → Child work packages
- Sub-tasks → Child work packages
- Issue links → OpenProject relationships

## Next Steps
1. Run full dry-run test
2. Analyze migration logs
3. Fix any mapping issues
4. Prepare for production migration
5. Schedule downtime
6. Execute migration
7. Verify data integrity

## Documentation References
- [README.md](./README.md): Project setup and technical overview
- [Configuration Guide](./docs/configuration.md): Detailed configuration options
