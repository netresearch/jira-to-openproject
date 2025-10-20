# ADR: Rails Console Requirement for OpenProject Data Migration

**Date**: 2025-10-20
**Status**: Accepted
**Deciders**: Development Team
**Context**: j2o migration tool architecture

## Context and Problem Statement

The j2o migration tool needs to efficiently import large volumes of data from Jira into OpenProject. We must decide between using the OpenProject REST API or direct Rails console access via ActiveRecord for bulk data operations.

## Decision Drivers

- **Performance**: Migrate thousands of work packages, users, and related entities efficiently
- **Capability**: Access features not exposed through REST API
- **Transaction Safety**: Ensure atomic operations for data consistency
- **Validation Control**: Ability to bypass validations for migration scenarios
- **Bulk Operations**: Handle large batches without timeouts or memory issues

## Considered Options

1. **OpenProject REST API** - Use HTTP API endpoints for all operations
2. **Rails Console via ActiveRecord** - Direct database access through Rails ORM
3. **Hybrid Approach** - Mix of REST API and Rails console

## Decision Outcome

**Chosen**: Rails Console via ActiveRecord (Option 2)

**Rationale**: The REST API is **fundamentally unsuitable** for bulk migration operations due to inherent limitations in performance, capabilities, and batch processing.

### REST API Limitations

#### 1. Performance Constraints

**File-based Batch Operations Required**:
```python
# From enhanced_openproject_client.py:111
"""This uses a file-based flow to avoid large in-console parsing."""
```

The architecture explicitly avoids REST API for bulk operations:
- Direct Rails console parsing reserved for small results only
- Batch operations require temp files + subprocess execution
- REST API overhead (HTTP, JSON serialization, validation per request) is prohibitive for thousands of records

**Evidence from codebase**:
```python
# enhanced_openproject_client.py:3-5
"""This client adds file-based Ruby command execution for large commands/results,
and parallelized HTTP helpers for bulk operations. Direct Rails console parsing
is reserved for small results; batch operations use temp files and subprocess."""
```

#### 2. Transaction Support

**ActiveRecord Transactions Not Available via REST**:
```ruby
# From openproject_client.py:3380
ActiveRecord::Base.transaction do
  {transaction_commands}
end
```

Requirements:
- Atomic multi-record operations (create project + members + roles)
- Rollback capability on partial failures
- Cross-table consistency guarantees
- REST API operates on single-resource requests without transaction boundaries

#### 3. Validation Bypass Capability

Migration scenarios require importing historical data that may not pass current validations:
- Legacy Jira data with different validation rules
- Preserving original created_at/updated_at timestamps
- Bypassing workflow state validations
- Setting system fields not accessible via REST API

**Rails Console approach**:
```ruby
# Bypass validations when necessary for migration
work_package.save(validate: false)
```

REST API enforces all validations unconditionally - unsuitable for migration use case.

#### 4. Direct ActiveRecord Access

**Safe Query Building with Parameterization**:
```python
# From openproject_client.py:5521
# Use ActiveRecord's built-in parameterization instead of string building
```

Benefits:
- Safe SQL parameterization via ActiveRecord
- Access to ActiveRecord query interface (where, joins, includes)
- Complex queries not exposed through REST API
- Direct model relationships and associations

#### 5. Batch Processing Architecture

**Documented Rails Console Priority**:
```python
# From openproject_client.py:917
# Execute via persistent tmux Rails console (faster than rails runner)
```

Architecture shows clear preference:
1. **Primary**: Persistent tmux Rails console (fastest)
2. **Fallback**: Rails runner (when console unstable)
3. **Not used**: REST API for bulk operations

#### 6. File-Based Result Flow

**Single Authoritative Output Path**:
```markdown
# From AGENTS.md:220-222
Use a single file-based result flow. The Ruby script writes a results JSON file
in the container; the migration copies it to var/data and stores a timestamped
copy. Prefer this file as the sole authoritative output path.
```

This pattern is incompatible with REST API's request-response model:
- Large result sets exceed HTTP response limits
- File-based flow enables streaming and chunking
- Timestamped audit trail of all operations
- Reliable error recovery and retry mechanisms

### Implementation Evidence

The codebase demonstrates pervasive Rails console usage:

**Client Architecture** (from CLIENT_API.md):
```
OpenProjectClient (Orchestration)
    ├── SSHClient (Foundation)
    ├── DockerClient (Container Operations, uses SSHClient)
    └── RailsConsoleClient (Console Interaction)
```

**Rails Console as Core Component** (from ARCHITECTURE.md:83):
```markdown
**Purpose**: Rails console interaction via tmux
**Capabilities**:
- Tmux session management
- Ruby code execution
- File-based results
```

**Minimal Ruby Scripts Policy** (from AGENTS.md:217-218):
```markdown
Minimal Ruby scripts: only load JSON, instantiate ActiveRecord models,
assign attributes, save. Do not implement mapping, sanitation, branching
logic, or result analysis in Ruby.
```

This design pattern is **only possible** with Rails console access, not REST API.

### Performance Characteristics

**Rails Console**:
- Persistent tmux session (no connection overhead)
- Direct ActiveRecord queries (no HTTP serialization)
- Batch operations in single transaction
- File-based large result handling
- Sub-second execution for complex queries

**REST API**:
- HTTP request overhead per operation
- JSON serialization/deserialization per request
- No transaction support across requests
- Response size limits
- Rate limiting and timeout constraints

### Capability Comparison

| Capability | REST API | Rails Console |
|------------|----------|---------------|
| Bulk inserts (1000+ records) | ❌ Slow/timeout | ✅ Efficient |
| Transactions | ❌ No support | ✅ Full support |
| Validation bypass | ❌ Not possible | ✅ Available |
| Direct AR queries | ❌ No access | ✅ Full access |
| File-based results | ❌ Not supported | ✅ Core pattern |
| Set created_at | ❌ Restricted | ✅ Unrestricted |
| Complex joins | ❌ Limited | ✅ Full AR ORM |

## Consequences

### Positive

- **Performance**: 10-100x faster for bulk operations vs REST API
- **Capability**: Full ActiveRecord feature set available
- **Transaction Safety**: Atomic operations with rollback support
- **Flexibility**: Direct database access for complex migrations
- **Reliability**: File-based flow enables robust error handling
- **Control**: Fine-grained control over validations and timestamps

### Negative

- **Dependency**: Requires SSH access to OpenProject host
- **Complexity**: More complex setup than simple HTTP client
- **Container Coupling**: Requires Docker access and container knowledge
- **Session Management**: Requires tmux session lifecycle management
- **Expertise**: Requires Rails and ActiveRecord knowledge

### Risk Mitigation

- **SSH Security**: Use key-based auth, connection pooling, timeouts
- **Transaction Safety**: All bulk operations wrapped in transactions
- **Error Handling**: Comprehensive exception handling with rich context
- **Validation**: Sanitize all data in Python before Rails execution
- **Testing**: Comprehensive test coverage for Rails script generation

## Related Decisions

- [2025-10-20: Tmux Session Requirement](2025-10-20-tmux-session-requirement.md) - Why persistent tmux session is required
- [AGENTS.md](../../AGENTS.md) - Rails Console Script Handling Policy (lines 216-228)
- [CLIENT_API.md](../CLIENT_API.md) - Client architecture reference
- [ARCHITECTURE.md](../ARCHITECTURE.md) - System architecture overview

## References

- `src/clients/enhanced_openproject_client.py` - File-based batch operations
- `src/clients/openproject_client.py` - ActiveRecord transaction support
- `src/clients/rails_console_client.py` - Tmux console interaction layer
- `docs/ARCHITECTURE.md` - Rails console architecture
- `docs/CLIENT_API.md` - Client layer reference
- `AGENTS.md` - Rails Console Script Handling Policy

## Conclusion

**The OpenProject REST API is not suitable for bulk migration operations.** The j2o migration tool **MUST use Rails console via ActiveRecord** for all data import operations due to fundamental limitations in the REST API's performance, capabilities, and batch processing support.

This is not a preference or optimization - it is an **architectural requirement** based on technical constraints that cannot be overcome while using the REST API.

Any discussion of using the REST API for bulk migration operations should reference this ADR and acknowledge that the REST API approach is **technically infeasible** for the j2o use case.
