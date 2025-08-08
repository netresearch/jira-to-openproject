# Data Preservation Safeguards

## Overview

The Data Preservation Safeguards system is a comprehensive solution designed to protect manually imported or modified data in OpenProject during Jira-to-OpenProject migrations. This system ensures that user modifications are preserved while allowing for seamless data synchronization between the two platforms.

## Key Features

### 1. Manual Data Detection
- **Automatic Detection**: Identifies when data has been manually modified in OpenProject
- **Change Tracking**: Compares current OpenProject data against original migration state
- **Field-Level Analysis**: Detects changes at the individual field level

### 2. Conflict Detection
- **Intelligent Comparison**: Detects conflicts between Jira changes and OpenProject modifications
- **Field-Specific Rules**: Applies different comparison logic for different field types
- **Normalization Support**: Handles case sensitivity, whitespace, and data formatting differences

### 3. Precedence Rules
- **Configurable Strategies**: Supports multiple conflict resolution strategies:
  - `JIRA_WINS`: Jira data takes precedence
  - `OPENPROJECT_WINS`: OpenProject data takes precedence
  - `MERGE`: Intelligent merging of conflicting data
  - `PROMPT_USER`: User intervention for conflict resolution
  - `SKIP`: Skip conflicting updates

### 4. Merge Capabilities
- **Multiple Merge Strategies**:
  - `LATEST_TIMESTAMP`: Use the most recent value
  - `LONGEST_VALUE`: Use the longest/most detailed value
  - `CONCATENATE`: Combine conflicting values
  - `CUSTOM`: Custom merge logic

### 5. Preservation Policies
- **Per-Entity Configuration**: Different policies for users, projects, issues, etc.
- **Field-Level Protection**: Specify which fields should be protected
- **Dynamic Updates**: Policies can be updated without system restart

## Architecture

### Core Components

#### DataPreservationManager
The main class that orchestrates all data preservation operations.

```python
from src.utils.data_preservation_manager import DataPreservationManager

# Initialize with configuration
preservation_manager = DataPreservationManager(config_manager, preservation_dir)
```

#### Entity Change Types
```python
from src.utils.data_preservation_manager import EntityChangeType

# Available change types:
# - CREATED: Entity was created after initial migration
# - UPDATED: Entity was modified after initial migration
# - DELETED: Entity was deleted after initial migration
# - UNCHANGED: No changes detected
# - CONFLICT: Conflicts detected between Jira and OpenProject
```

#### Conflict Resolution Strategies
```python
from src.utils.data_preservation_manager import ConflictResolution

# Available strategies:
# - JIRA_WINS: Jira data takes precedence
# - OPENPROJECT_WINS: OpenProject data takes precedence
# - MERGE: Intelligent merging
# - PROMPT_USER: User intervention
# - SKIP: Skip conflicting updates
```

#### Merge Strategies
```python
from src.utils.data_preservation_manager import MergeStrategy

# Available strategies:
# - LATEST_TIMESTAMP: Use most recent value
# - LONGEST_VALUE: Use longest/most detailed value
# - CONCATENATE: Combine values
# - CUSTOM: Custom logic
```

## Usage Examples

### Basic Usage

```python
# Initialize the preservation manager
preservation_manager = DataPreservationManager(config_manager)

# Store original state before migration
preservation_manager.store_original_state("user_123", "users", user_data)

# Detect changes in OpenProject
change_type = preservation_manager.detect_openproject_changes(
    "user_123", "users", current_op_data
)

# Analyze preservation status for batch operations
report = preservation_manager.analyze_preservation_status(
    jira_changes, "users", openproject_client
)
```

### Conflict Resolution

```python
# Detect conflicts
conflicts = preservation_manager.detect_conflicts(jira_data, op_data, "users")

# Resolve conflicts
resolved_data = preservation_manager.resolve_conflict(
    conflict_info, jira_data, op_data
)
```

### Policy Management

```python
# Get current policy
policy = preservation_manager.get_preservation_policy("users")

# Update policy
new_policy = {
    "resolution_strategy": ConflictResolution.MERGE,
    "protected_fields": ["firstname", "lastname", "email", "phone"],
    "allow_merge": True,
}
preservation_manager.update_preservation_policy("users", new_policy)
```

## Configuration

### Default Policies

The system comes with pre-configured policies for common entity types:

#### Users
```json
{
    "resolution_strategy": "openproject_wins",
    "merge_strategy": "latest_timestamp",
    "protected_fields": ["firstname", "lastname", "email", "title", "department"],
    "allow_merge": true,
    "backup_before_update": true,
    "notify_on_conflict": true
}
```

#### Projects
```json
{
    "resolution_strategy": "openproject_wins",
    "merge_strategy": "latest_timestamp",
    "protected_fields": ["name", "description", "status"],
    "allow_merge": true,
    "backup_before_update": true,
    "notify_on_conflict": true
}
```

#### Issues/Work Packages
```json
{
    "resolution_strategy": "merge",
    "merge_strategy": "latest_timestamp",
    "protected_fields": ["subject", "description", "status", "priority"],
    "allow_merge": true,
    "backup_before_update": true,
    "notify_on_conflict": true
}
```

#### Comments
```json
{
    "resolution_strategy": "openproject_wins",
    "merge_strategy": "concatenate",
    "protected_fields": ["comment"],
    "allow_merge": false,
    "backup_before_update": true,
    "notify_on_conflict": true
}
```

### Custom Policies

You can create custom policies by adding a `custom_policies.json` file in the preservation directory:

```json
{
    "custom_entity": {
        "resolution_strategy": "merge",
        "merge_strategy": "concatenate",
        "protected_fields": ["custom_field"],
        "allow_merge": true,
        "backup_before_update": true,
        "notify_on_conflict": false
    }
}
```

## Advanced Features

### Backup and Restore

```python
# Create backup before updates
backup_path = preservation_manager.create_backup("user_123", "users", current_data)

# Restore from backup if needed
restored_data = preservation_manager.restore_from_backup(backup_path)
```

### Performance Optimization

The system includes performance optimizations for large datasets:

- **Batch Processing**: Processes entities in configurable batches
- **Progress Tracking**: Real-time progress updates with Rich console
- **Efficient Storage**: Optimized file storage and retrieval
- **Memory Management**: Efficient memory usage for large datasets

### Error Handling

Comprehensive error handling ensures system reliability:

- **Graceful Degradation**: Continues operation even with partial failures
- **Corruption Recovery**: Handles corrupted state files gracefully
- **Missing Data Handling**: Manages missing or invalid data appropriately
- **Logging**: Detailed logging for troubleshooting

### Analytics and Reporting

```python
# Get preservation summary
summary = preservation_manager.get_preservation_summary()

# Summary includes:
# - Total entities tracked
# - Entities by type
# - Recent conflicts
# - Backup count
```

## Integration with Migration System

The data preservation system is fully integrated with the migration workflow:

### Base Migration Integration

```python
# In base_migration.py
def run_with_data_preservation(self, entity_type: str, jira_data: Dict[str, Any]) -> Dict[str, Any]:
    """Run migration with data preservation safeguards."""
    
    # Store original state
    self.preservation_manager.store_original_state(entity_id, entity_type, original_data)
    
    # Detect conflicts
    conflicts = self.preservation_manager.detect_conflicts(jira_data, op_data, entity_type)
    
    if conflicts:
        # Resolve conflicts
        resolved_data = self.preservation_manager.resolve_conflict(conflict, jira_data, op_data)
        return resolved_data
    
    return jira_data
```

### Migration Workflow

1. **Initial Migration**: Store original state of all migrated entities
2. **Subsequent Updates**: Detect changes and apply preservation rules
3. **Conflict Resolution**: Automatically resolve conflicts based on policies
4. **Backup Creation**: Create backups before any modifications
5. **Audit Trail**: Maintain complete audit trail of all operations

## Testing

The system includes comprehensive test coverage:

### Unit Tests
- Individual component testing
- Policy validation
- Conflict detection accuracy
- Merge strategy validation

### Integration Tests
- End-to-end workflow testing
- Performance testing with large datasets
- Error handling validation
- Backup and restore functionality

### Test Coverage Areas
- Manual data detection scenarios
- Conflict detection accuracy
- Precedence rules application
- Merge capabilities
- Preservation policy configuration
- Integration with migration workflow
- Backup and restore functionality
- Error handling and recovery
- Performance with large datasets
- Comprehensive workflow validation

## Best Practices

### Configuration
1. **Review Default Policies**: Understand default policies before customization
2. **Test Custom Policies**: Test custom policies in development environment
3. **Monitor Performance**: Monitor system performance with large datasets
4. **Regular Backups**: Ensure regular backups of preservation data

### Usage
1. **Store Original State**: Always store original state before first migration
2. **Monitor Conflicts**: Regularly review conflict reports
3. **Update Policies**: Update policies based on business requirements
4. **Test Scenarios**: Test various conflict scenarios

### Maintenance
1. **Cleanup Old Data**: Regularly clean up old backup files
2. **Monitor Storage**: Monitor preservation directory size
3. **Review Logs**: Regularly review system logs for issues
4. **Update Policies**: Update policies as business needs change

## Troubleshooting

### Common Issues

#### High Conflict Count
- Review preservation policies
- Check field normalization settings
- Verify data consistency between systems

#### Performance Issues
- Reduce batch size
- Optimize storage location
- Review logging levels

#### Storage Issues
- Clean up old backups
- Compress preservation data
- Move to larger storage

### Debug Information

Enable debug logging for detailed troubleshooting:

```python
import logging
logging.getLogger('src.utils.data_preservation_manager').setLevel(logging.DEBUG)
```

## Future Enhancements

### Planned Features
- **Web UI**: Web-based interface for policy management
- **Advanced Analytics**: Enhanced reporting and analytics
- **Machine Learning**: ML-based conflict resolution
- **Real-time Monitoring**: Real-time conflict monitoring
- **API Integration**: REST API for external integration

### Extension Points
- **Custom Merge Strategies**: Plugin system for custom merge logic
- **External Integrations**: Integration with external systems
- **Advanced Policies**: More sophisticated policy rules
- **Performance Optimizations**: Additional performance improvements

## Conclusion

The Data Preservation Safeguards system provides a robust, flexible, and efficient solution for protecting manually modified data during Jira-to-OpenProject migrations. With comprehensive conflict detection, intelligent resolution strategies, and extensive configuration options, it ensures data integrity while maintaining system performance and reliability.
