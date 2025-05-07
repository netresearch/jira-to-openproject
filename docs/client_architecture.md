# OpenProject Client Architecture

## Overview

This document outlines the architecture for the OpenProject client system, which provides a clean API for interacting with an OpenProject instance via Rails console. The client architecture follows a layered design pattern with clear component separation and dependency injection.

## Architecture Diagram

```plain
┌─────────────────────────────────── LOCAL SYSTEM ───────────────────────────────────┐
│                                                                                    │
│  ┌────────────────────────────────────────────┐                                    │
│  │              OpenProjectClient             │                                    │
│  │   (High-level API & Main Orchestrator)     │                                    │
│  └───┬──────────────┬─────────────────┬───────┘                                    │
│      │              │                 │                                            │
│      │ ---owns---   │ ---owns----     │ ---owns---                                 │
│      ▼              ▼                 ▼                                            │
│  ┌────────────┐ ┌────────────┐ ┌────────────────────┐                              │
│  │ SSHClient  │ │DockerClient│ │ RailsConsoleClient │                              │
│  └──────┬─────┘ └─────┬──────┘ └─────────┬──────────┘                              │
│         │ ▲           │                  │                                         │
│         │ └---uses----┘                  │                                         │
│         │                                │                                         │
└─────────┼────────────────────────────────┼─────────────────────────────────────────┘
          │                                │
          │ .....SSH........               │ .....Local tmux.....
          │ .....Connection....            │ .....Session........
          ▼                                │
┌───────────────────────────────────────────────────────────────────────────────────────┐
│                                REMOTE SYSTEM                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐          │
│  │                         Docker Container                                │          │
│  │   ┌─────────────────────────────────────────────────────────────────┐   │          │
│  │   │                      Rails Console                              │◄..┼..........┘
│  │   │                                                                 │   │
│  │   │   ┌────────────┐     ┌────────────┐     ┌────────────────────┐  │   │
│  │   │   │ Ruby Script│....►│Result Files│◄....│Executed Ruby Code  │  │   │
│  │   │   └────────────┘     └─────┬──────┘     └────────────────────┘  │   │
│  │   └────────────────────────────┼────────────────────────────────────┘   │
│  │                                │                                        │
│  └────────────────────────────────┼────────────────────────────────────────┘
│                                   │                                         │
└───────────────────────────────────┼─────────────────────────────────────────┘
                                    │
                                    │ .....File Transfer.....
                                    │ .....(via SSH+Docker)...
                                    │
                                    ▼
                            Back to LOCAL SYSTEM
                            for result processing
```

## Component Responsibilities

### 1. SSHClient (Foundation Layer)

- Serves as the centralized base component for all SSH operations
- Handles direct SSH connections to remote server
- Executes remote commands via SSH
- Transfers files to/from remote server via SCP
- Implements connection pooling and automatic reconnect logic
- Provides robust error handling and retry mechanisms
- **Recent Improvements**: Refactored to serve as the foundation layer with enhanced connection management and robust retry policies (Task #21)

Example usage:
```python
# Creating an SSHClient
ssh_client = SSHClient(
    host="remote-server.example.com",
    user="username",
    key_file="/path/to/key",
    retry_count=3,
    retry_delay=1.0
)

# Executing a command
result = ssh_client.execute_command("ls -la")
if result["status"] == "success":
    print(result["stdout"])

# Transferring files
ssh_client.copy_file_to_remote("/local/path/file.txt", "/remote/path/file.txt")
```

### 2. DockerClient (Container Interaction Layer)

- Manages Docker container operations
- **Uses SSHClient** to execute Docker commands remotely (dependency injection)
- Copies files to/from Docker container
- Provides container verification
- **Recent Improvements**: Modified to accept an SSHClient parameter in its constructor rather than creating its own (Task #22)

Example usage:
```python
# Creating a DockerClient with an existing SSHClient
docker_client = DockerClient(
    container_name="openproject-web-1",
    ssh_client=ssh_client  # Dependency injection
)

# Executing a command in the container
result = docker_client.execute_command("bundle exec rake db:migrate")

# Transferring files to container
docker_client.copy_file_to_container("/local/path/script.rb", "/app/script.rb")
```

### 3. RailsConsoleClient (Console Interaction Layer)

- Interacts with Rails console via local tmux
- Executes Ruby code in Rails console via tmux sessions
- Uses robust marker-based output capture for reliable result extraction
- Implements advanced error detection with unique markers
- **Recent Improvements**:
  - Simplified to focus only on tmux session interactions (Task #23)
  - Enhanced execute method with direct output capture using unique markers (Task #24)
  - Optimized performance with:
    - Console prompt detection (avoids unnecessary stabilization)
    - Adaptive polling (starts at 0.05s and scales up to 0.5s based on output changes)
    - Minimal wait times between operations (0.1s vs previous 0.5-1.0s)
    - Smart error handling with pattern detection for common Ruby errors
  - Provides detailed debug logs for troubleshooting
  - Independent of SSH and Docker concerns (clean separation of responsibilities)

Example usage:
```python
# Creating a RailsConsoleClient
rails_client = RailsConsoleClient(
    tmux_session_name="rails_console",
    command_timeout=180
)

# Executing Ruby code
result = rails_client.execute('''
  # Ruby code to execute
  projects = Project.count
  puts "Found #{projects} projects"
  "SUCCESS: #{projects} projects"
''')

if result["status"] == "success":
    print(f"Command succeeded with output: {result['output']}")
else:
    print(f"Command failed with error: {result['error']}")
```

### 4. OpenProjectClient (Orchestration Layer)

- Provides high-level API for OpenProject operations
- Orchestrates the entire workflow
- **Owns and initializes** all other clients in the correct hierarchical order
- Creates Ruby scripts and processes results
- **Recent Improvements**:
  - Updated to act as the top-level component that initializes all clients (Task #25)
  - Refactored file transfer methods to use SSHClient and DockerClient consistently (Task #26)

Example usage:
```python
# Creating an OpenProjectClient (initializes all layers)
client = OpenProjectClient(
    container_name="openproject-web-1",
    ssh_host="remote-server.example.com",
    ssh_user="username",
    tmux_session_name="rails_console"
)

# Using high-level APIs
projects = client.count_records("Project")
users = client.find_all_records("User", conditions={"admin": True})

# Executing custom queries
result = client.execute_query('Project.find_by(identifier: "example").name')
```

## Error Detection System

One of the key improvements in the recent refactoring was enhancing the RailsConsoleClient's error detection system. When executing commands in the Rails console via tmux, distinguishing between actual Ruby errors and error text in command outputs was challenging.

### Marker-Based Solution

The improved system uses unique markers and a precise extraction method:

1. **Command Wrapping**: Each command is wrapped with start/end/error markers using a unique ID
   ```ruby
   # Print start marker
   puts "START" "unique_id"  # Rendered as: STARTunique_id

   begin
     result = <user command>
     # Print the result and end marker
     puts result.inspect
     puts "END" "unique_id"  # Rendered as: ENDunique_id
   rescue => e
     # Print error marker and details
     puts "ERROR" "unique_id"  # Rendered as: ERRORunique_id
     puts "Ruby error: #{e.class}: #{e.message}"
     puts "END" "unique_id"
   end
   ```

2. **Output Extraction**: The client extracts content between START and END markers
3. **Error Detection Priority**: Checks for error markers first, then success indicators
4. **Unique Marker Pattern**: Uses concatenated string arguments to create markers that are distinguishable from regular text (`puts "START" "id"` renders as `STARTid` without spaces)

This system reliably distinguishes between:
- Actual Ruby errors occurring during execution
- Text containing words like "ERROR" or "error" in command inputs or outputs
- Success indicators in command results

## Data Flow Process

1. **Client Initialization**:
   - OpenProjectClient initializes SSHClient
   - SSHClient is passed to DockerClient via constructor
   - RailsConsoleClient is initialized for tmux interaction

2. **Script Execution**:
   - OpenProjectClient creates Ruby script locally
   - SSHClient transfers script to remote host
   - DockerClient copies script into container
   - RailsConsoleClient executes the script in Rails console
   - Results are extracted with marker-based detection
   - OpenProjectClient processes and returns results

3. **File Transfer**:
   - Files are transferred to remote server via SSHClient
   - Files are moved to/from container via DockerClient
   - Each operation has configurable retries and error handling

## Recent Refactoring Summary

The client architecture underwent significant refactoring in Tasks #21-26:

1. **SSHClient** was established as the foundation component with improved connection handling and retry logic
2. **DockerClient** was modified to accept an SSHClient parameter instead of creating its own
3. **RailsConsoleClient** was simplified to focus only on tmux session interactions
4. **OpenProjectClient** was updated to properly own and initialize all clients in the correct hierarchy
5. **Error detection** was enhanced with a more robust marker-based system
6. **File transfer methods** were refactored for consistency and reliability

These improvements have resulted in:
- Cleaner component separation with single responsibilities
- Proper dependency injection pattern
- More reliable error detection and handling
- Consistent retry mechanisms
- Better testability of individual components

## Future Considerations

- Consider implementing a configuration system for client settings
- Add telemetry and performance monitoring
- Implement connection pooling for high-volume operations
- Add support for non-tmux Rails console access methods

## Further Reading

- Related Tasks: #21, #22, #23, #24, #25, #26
- Test files: `test_rails_connection.py`, `test_error_marker.py`
- Source code: `src/clients/`
