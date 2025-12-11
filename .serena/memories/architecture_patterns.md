# Architecture Patterns and Design Guidelines

## Client Layer Hierarchy

### Layered Architecture
```
OpenProjectClient (Orchestration Layer)
    ├── uses → SSHClient (Foundation Layer)
    ├── uses → DockerClient (Container Operations, uses SSHClient)
    └── uses → RailsConsoleClient (Console Interaction)
```

### Client Responsibilities

#### SSHClient (Foundation)
- Establishes and manages SSH connections
- Executes remote commands
- Handles file transfers
- Connection pooling and reuse
- Error handling for network issues

#### DockerClient (Container Operations)
- Container lifecycle management via SSH
- File transfers to/from containers
- Command execution inside containers
- Depends on SSHClient for remote access

#### RailsConsoleClient (Console Interaction)
- Manages tmux session for Rails console
- Executes Ruby code in Rails context
- Handles console I/O and polling
- File-based result retrieval
- Session lifecycle management

#### OpenProjectClient (Orchestration)
- High-level migration operations
- Coordinates other clients
- Rails script generation and execution
- Result parsing and validation
- Error aggregation and reporting

## Exception Hierarchy

### Base Exception Classes
```
Exception (Python standard)
├── SSHConnectionError          # SSH connection failures
├── SSHCommandError             # SSH command execution failures
├── SSHFileTransferError        # SSH file transfer failures
├── RailsConsoleError           # Base Rails console error
│   ├── TmuxSessionError        # tmux session issues
│   ├── ConsoleNotReadyError    # Console not ready for commands
│   └── CommandExecutionError   # Command execution failures
└── OpenProjectError            # Base OpenProject error
    ├── ConnectionError         # API connection failures
    ├── QueryExecutionError     # Query execution failures
    ├── RecordNotFoundError     # Record lookup failures
    └── JsonParseError          # JSON parsing failures
```

### Exception Design Principles
1. **Specific exceptions**: Use specific exception types, not generic Exception
2. **Context-rich**: Include diagnostics in exception messages
3. **Chaining**: Use `raise ... from e` to preserve exception chain
4. **No return codes**: Never return error status, always raise

## Migration Pipeline Pattern

### BaseMigration Abstract Class
All migration components inherit from `BaseMigration`:

```python
class BaseMigration(ABC):
    @abstractmethod
    def extract(self) -> list[dict[str, Any]]:
        """Extract data from Jira API."""
        pass
    
    @abstractmethod
    def map(self, jira_data: list[dict]) -> list[dict[str, Any]]:
        """Transform Jira data to OpenProject format."""
        pass
    
    @abstractmethod
    def load(self, openproject_data: list[dict]) -> None:
        """Load data into OpenProject via Rails console."""
        pass
```

### Extract Phase
- Fetch data from Jira REST API
- Handle pagination
- Apply filters and queries
- Cache results to JSON files under `var/data/`
- Use tenacity for retry logic

### Map Phase
- Transform Jira data structures to OpenProject format
- Apply mappings from `config.mappings`
- Sanitize data (remove `_links`, flatten IDs)
- Validate required fields
- Preserve provenance metadata (J2O_* fields)

### Load Phase
- Generate Rails console Ruby scripts
- Execute via RailsConsoleClient
- Parse results from JSON files
- Handle idempotency
- Update mapping files

## Rails Console Integration Patterns

### Script Generation Pattern
Split scripts into parameterized head and literal body:

```python
def generate_rails_script(self, data_path: str, results_path: str) -> str:
    # Head: interpolated parameters (f-string)
    head = f"""
results_file = '{results_path}'
data = JSON.parse(File.read('{data_path}'))
results = []
"""
    
    # Body: literal Ruby code (no interpolation)
    body = """
begin
  data.each do |item|
    record = Model.create!(item)
    results << {id: record.id, status: 'created'}
  end
  
  File.write(results_file, JSON.pretty_generate(results))
rescue => e
  error_data = {error: e.message, backtrace: e.backtrace[0..5]}
  File.write(results_file, JSON.pretty_generate(error_data))
  raise
end
"""
    
    return head + body
```

### File-Based Result Flow
1. Python generates JSON payload → `var/data/input.json`
2. Python generates Ruby script with file paths
3. Ruby script reads input JSON
4. Ruby creates ActiveRecord objects
5. Ruby writes results → container temp file
6. Python copies results → `var/data/results.json`
7. Python parses results and updates mappings

### Rails Script Requirements
- **Minimal Ruby**: Only load JSON, instantiate models, assign attributes, save
- **No logic in Ruby**: All mapping, sanitation, branching in Python
- **Idempotent**: Can be re-run safely
- **Error handling**: Catch and serialize errors to results file
- **File-based output**: Single authoritative result path

## Data Sanitization Pattern

### Before Rails Execution
Python must sanitize all JSON payloads:

```python
def _sanitize_wp_dict(self, wp_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove OpenProject API artifacts before Rails execution."""
    sanitized = wp_dict.copy()
    
    # Remove API-specific keys
    sanitized.pop('_links', None)
    sanitized.pop('_type', None)
    
    # Flatten IDs from _links structure
    if 'type' in wp_dict.get('_links', {}):
        sanitized['type_id'] = extract_id(wp_dict['_links']['type']['href'])
    
    # Ensure required AR attributes present
    if 'project_id' not in sanitized:
        raise ValueError("Missing required attribute: project_id")
    
    return sanitized
```

### Sanitization Rules
1. Remove `_links` objects (OpenProject API structure)
2. Remove `_type` metadata
3. Extract and flatten IDs from link hrefs
4. Ensure required ActiveRecord attributes present
5. Validate data types match ActiveRecord schema

## Resilience Patterns

### Retry Logic (tenacity)
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def fetch_jira_data(self, endpoint: str) -> dict:
    return self.jira_client.get(endpoint)
```

### Circuit Breaker (pybreaker)
```python
from pybreaker import CircuitBreaker

breaker = CircuitBreaker(fail_max=5, timeout_duration=60)

@breaker
def call_external_service(self):
    return self.external_client.request()
```

### Structured Logging (structlog)
```python
self.logger.info(
    "processing_batch",
    batch_size=len(items),
    component="users",
    start_index=start,
    end_index=end
)
```

## Idempotency Patterns

### JSON Caching
```python
def _load_from_json(self, file_path: str) -> list[dict]:
    """Load cached data if available."""
    if os.path.exists(file_path):
        self.logger.info("loading_cached_data", file_path=file_path)
        with open(file_path) as f:
            return json.load(f)
    return []

def _save_to_json(self, data: list[dict], file_path: str) -> None:
    """Cache data for idempotency."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=2)
```

### Checkpoint Database
SQLite database (`.migration_checkpoints.db`) tracks:
- Work package migration progress
- Batch processing state
- Fast-forward indicators
- Reset via `--reset-wp-checkpoints` flag

## Security Patterns

### Input Validation
```python
def _validate_jira_key(self, key: str) -> None:
    """Validate Jira key before use in queries or scripts."""
    if not key or not isinstance(key, str):
        raise ValueError("Jira key must be non-empty string")
    
    if len(key) > 100:
        raise ValueError(f"Jira key too long: {len(key)} chars")
    
    if not re.match(r'^[A-Z0-9\-]+$', key):
        raise ValueError(f"Invalid Jira key format: {key}")
```

### Safe Ruby Generation
```python
# ✅ CORRECT: Use Ruby inspect for safe escaping
ruby_code = f"jira_key = {jira_key!r}.inspect"

# ❌ WRONG: Direct interpolation (injection risk)
ruby_code = f"jira_key = '{jira_key}'"
```

## Configuration Management

### Configuration Precedence
1. Environment variables (highest priority)
2. `.env.local` (developer overrides)
3. `.env` (project defaults)
4. `config/config.yaml` (structured config)
5. Code defaults (lowest priority)

### Configuration Access
```python
from src.config import get_config

config = get_config()
batch_size = config.migration.batch_size
log_level = config.logging.level
```

## Testing Patterns

### Test Organization
```
tests/
├── unit/           # Fast, isolated, mocked dependencies
├── integration/    # External services (mocked)
├── end_to_end/     # Complete workflows
└── utils/          # Shared fixtures
```

### Test Markers
```python
@pytest.mark.unit
def test_validation():
    pass

@pytest.mark.integration
@pytest.mark.requires_docker
def test_docker_client():
    pass

@pytest.mark.slow
@pytest.mark.end_to_end
def test_full_migration():
    pass
```

## Compute Location Principle

**Python does computation, Ruby does minimum INSERT only.**

### Rationale
- Python has better parallelism (ThreadPoolExecutor, asyncio)
- Python debugging/logging is superior
- Ruby/Rails console has SSH/tmux overhead per call
- ActiveRecord is slow compared to raw SQL

### Application
```
❌ WRONG: Send minimal data → Ruby computes versions, ranges, snapshots
✅ RIGHT: Python pre-computes everything → Ruby just does INSERT

# Python should compute:
- Journal version numbers (1, 2, 3...)
- validity_period ranges (pre-calculated, non-overlapping)
- state_snapshot (full WP attribute dict)
- cf_state_snapshot (custom field values)
- field_changes (mapped to OP field names)

# Ruby should ONLY:
- Receive complete rows
- Execute bulk INSERT (raw SQL preferred over ActiveRecord)
- Return IDs/status
```

### Performance Impact
- Moves computation to Python's parallel threads
- Reduces Ruby iteration from O(journals) to O(1) per WP
- Amortizes SSH/tmux overhead across more data per call

---

## Key Design Principles

### SOLID Principles Applied
- **Single Responsibility**: Each migration handles one data type
- **Open/Closed**: BaseMigration open for extension, closed for modification
- **Liskov Substitution**: All migrations substitutable via BaseMigration
- **Interface Segregation**: Clients expose only needed interfaces
- **Dependency Inversion**: Depend on abstractions (BaseMigration, not concrete classes)

### Additional Patterns
- **DRY**: Shared utilities in `src/utils/`
- **KISS**: Simple, direct implementations
- **YAGNI**: No speculative features, remove legacy immediately
- **Optimistic Execution**: Try operation first, handle errors in exception handlers
- **Exception-Based Control**: No error return codes or status dictionaries
