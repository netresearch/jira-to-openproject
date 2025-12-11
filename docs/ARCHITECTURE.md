# System Architecture

## Overview

The Jira to OpenProject migration tool uses a layered client architecture for reliable data transfer and processing. The system operates on a remote OpenProject server via SSH, Docker containers, and Rails console interaction.

## Client Architecture

### Layer Hierarchy

```
OpenProjectClient (Orchestration Layer)
    ├── SSHClient (Foundation Layer)
    ├── DockerClient (Container Interaction Layer)
    └── RailsConsoleClient (Console Interaction Layer)
```

### Component Data Flow

```
LOCAL SYSTEM
    ↓ (Script Generation)
OpenProjectClient
    ↓ (File Transfer via SSH)
REMOTE SERVER
    ↓ (Copy to Container)
DOCKER CONTAINER
    ↓ (Execute in Rails Console)
RAILS APPLICATION
    ↓ (Process & Return Results)
Back to LOCAL SYSTEM
```

## Component Responsibilities

### SSHClient (Foundation)

**Purpose**: Centralized SSH operations and file transfer

**Key Features**:
- Direct SSH connections to remote server
- Remote command execution
- File transfer via SCP
- Connection pooling with automatic reconnect
- Robust error handling and retry mechanisms

**Usage**:
```python
ssh_client = SSHClient(
    host="remote.local",
    user="username",
    key_file="/path/to/key",
    retry_count=3
)

result = ssh_client.execute_command("ls -la")
ssh_client.copy_file_to_remote("/local/file.txt", "/remote/file.txt")
```

### DockerClient (Container Interaction)

**Purpose**: Docker container operations via SSH

**Key Features**:
- Uses SSHClient via dependency injection
- Container file transfer operations
- Docker command execution
- Container verification

**Usage**:
```python
docker_client = DockerClient(
    container_name="openproject-web-1",
    ssh_client=ssh_client  # Dependency injection
)

result = docker_client.execute_command("bundle exec rake db:migrate")
docker_client.copy_file_to_container("/local/script.rb", "/app/script.rb")
```

### RailsConsoleClient (Console Interaction)

**Purpose**: Rails console interaction via tmux

**Key Features**:
- Tmux session management
- Marker-based output capture for reliable result extraction
- Advanced error detection with unique markers
- Optimized performance with adaptive polling

**Error Detection System**:
Uses unique markers to distinguish Ruby errors from output text:

```ruby
# Command wrapping with markers
puts "START" "unique_id"  # Renders as: STARTunique_id
begin
  result = <user_command>
  puts result.inspect
  puts "END" "unique_id"
rescue => e
  puts "ERROR" "unique_id"
  puts "Ruby error: #{e.class}: #{e.message}"
  puts "END" "unique_id"
end
```

**Usage**:
```python
rails_client = RailsConsoleClient(
    tmux_session_name="rails_console",
    command_timeout=180
)

result = rails_client.execute('''
  projects = Project.count
  "SUCCESS: #{projects} projects"
''')
```

### OpenProjectClient (Orchestration)

**Purpose**: High-level API orchestration

**Key Features**:
- Initializes and coordinates all client layers
- Provides high-level API for OpenProject operations
- Handles Ruby script generation and result processing
- Manages complete workflow orchestration

**Usage**:
```python
client = OpenProjectClient(
    container_name="openproject-web-1",
    ssh_host="remote.local",
    ssh_user="username",
    tmux_session_name="rails_console"
)

projects = client.count_records("Project")
users = client.find_all_records("User", conditions={"admin": True})
```

## Exception Architecture

### Exception Hierarchy

```
Exception
├── SSHConnectionError, SSHCommandError, SSHFileTransferError
├── RailsConsoleError
│   ├── TmuxSessionError
│   ├── ConsoleNotReadyError
│   └── CommandExecutionError
│       └── RubyError
├── OpenProjectError
│   ├── ConnectionError
│   ├── QueryExecutionError
│   ├── RecordNotFoundError
│   └── JsonParseError
└── JiraError
    ├── JiraConnectionError
    ├── JiraAuthenticationError
    ├── JiraApiError
    ├── JiraResourceNotFoundError
    └── JiraCaptchaError
```

### Exception Propagation

1. **Low-level exceptions** (SSH, Docker) propagate upward through layers
2. **Each layer** can catch and translate exceptions to appropriate high-level types
3. **Context preservation** ensures original error information is maintained
4. **Application-level** code should catch OpenProjectError or JiraError types

## Performance Optimizations

### RailsConsoleClient Optimizations

- **Console prompt detection**: Avoids unnecessary stabilization waits
- **Adaptive polling**: Starts at 0.05s, scales to 0.5s based on output changes
- **Minimal wait times**: 0.1s between operations (vs previous 0.5-1.0s)
- **Smart error handling**: Pattern detection for common Ruby errors

### Connection Management

- **Connection pooling** for SSH connections
- **Automatic reconnection** with configurable retry policies
- **Session reuse** for tmux console interactions

## Migration Data Processing

### Workflow Process

1. **Client Initialization**: OpenProjectClient initializes all dependent clients
2. **Script Generation**: Ruby scripts created locally based on migration requirements
3. **File Transfer**: Scripts transferred to remote server via SSHClient
4. **Container Operations**: Scripts moved into Docker container via DockerClient
5. **Console Execution**: Scripts executed in Rails console via RailsConsoleClient
6. **Result Processing**: Results extracted with marker-based detection and returned

### Error Handling Strategy

- **Optimistic execution**: Attempt operations first, validate in exception handlers
- **Layered error handling**: Each layer provides appropriate error context
- **Comprehensive logging**: Detailed debug information for troubleshooting
- **Graceful degradation**: Fallback strategies for network or service issues

## Security Considerations

### Input Validation

All user inputs, especially Jira keys, are validated to prevent injection attacks:

- **Character whitelisting**: Only `A-Z`, `0-9`, and `-` allowed
- **Length limits**: Maximum 100 characters
- **Control character blocking**: Prevents newlines, null bytes, etc.

### Output Escaping

Dynamic content uses Ruby's `inspect` method for safe string formatting in generated scripts.

This architecture provides reliable, maintainable migration processing with proper separation of concerns and robust error handling.

## Related Documentation

- [Entity Mapping Reference](ENTITY_MAPPING.md) - Comprehensive Jira→OpenProject field mappings
- [Migration Components Catalog](MIGRATION_COMPONENTS.md) - Module listing with development state
- [Architecture Decisions](adr/) - ADRs documenting key technical decisions
- [Client API Reference](CLIENT_API.md) - Detailed client layer documentation
- [Developer Guide](DEVELOPER_GUIDE.md) - Development standards and practices
