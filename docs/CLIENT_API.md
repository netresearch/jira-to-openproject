# Client Layer API Reference

**Version**: 1.0
**Last Updated**: 2025-10-14

## Overview

The j2o migration tool uses a layered client architecture for reliable remote operations. This document provides comprehensive API reference for all client classes.

### Architecture

```
OpenProjectClient (Orchestration)
    ├── SSHClient (Foundation)
    ├── DockerClient (Container Operations, uses SSHClient)
    └── RailsConsoleClient (Console Interaction)
```

**Design Principles**:
- Exception-based error handling (no return codes)
- Optimistic execution (validate in exception handlers)
- Connection pooling and retry logic
- Comprehensive logging with structlog

**Related Documentation**:
- [Architecture Overview](ARCHITECTURE.md)
- [Security Guidelines](SECURITY.md)
- [Developer Guide](DEVELOPER_GUIDE.md)

---

## SSHClient

Foundation layer for SSH connections and remote command execution.

**Location**: `src/clients/ssh_client.py`

### Class: SSHClient

#### Constructor

```python
def __init__(
    self,
    host: str,
    user: str,
    key_file: str | None = None,
    password: str | None = None,
    connect_timeout: int = 30,
    operation_timeout: int = 300,
    retry_count: int = 3,
    retry_delay: float = 2.0,
    auto_reconnect: bool = True,
    file_manager=None
)
```

**Parameters**:
- `host`: Remote server hostname or IP
- `user`: SSH username
- `key_file`: Path to SSH private key file (optional)
- `password`: SSH password (optional, prefer key-based auth)
- `connect_timeout`: Connection timeout in seconds (default: 30)
- `operation_timeout`: Operation timeout in seconds (default: 300)
- `retry_count`: Number of retry attempts (default: 3)
- `retry_delay`: Delay between retries in seconds (default: 2.0)
- `auto_reconnect`: Automatically reconnect on connection loss (default: True)
- `file_manager`: File manager instance for coordinated file operations

**Example**:
```python
from src.clients.ssh_client import SSHClient

client = SSHClient(
    host="openproject.example.com",
    user="admin",
    key_file="/path/to/private_key",
    connect_timeout=30,
    retry_count=3
)
```

#### Methods

##### connect()

Establish SSH connection to remote host.

```python
def connect(self) -> None
```

**Raises**:
- `SSHConnectionError`: Connection failed after retries

**Example**:
```python
try:
    client.connect()
except SSHConnectionError as e:
    logger.error("Connection failed", error=str(e))
```

##### test_connection()

Test SSH connection with echo command.

```python
def test_connection(self) -> bool
```

**Returns**: `True` if connection successful, `False` otherwise

**Example**:
```python
if client.test_connection():
    logger.info("Connection verified")
```

##### execute_command()

Execute command on remote host via SSH.

```python
def execute_command(
    self,
    command: str,
    timeout: int | None = None,
    check_returncode: bool = True,
    capture_output: bool = True
) -> tuple[int, str, str]
```

**Parameters**:
- `command`: Shell command to execute
- `timeout`: Command timeout in seconds (default: operation_timeout)
- `check_returncode`: Raise exception on non-zero return code
- `capture_output`: Capture stdout/stderr

**Returns**: Tuple of (return_code, stdout, stderr)

**Raises**:
- `SSHCommandError`: Command execution failed
- `SSHConnectionError`: Connection lost during execution

**Example**:
```python
returncode, stdout, stderr = client.execute_command(
    "docker ps -a",
    timeout=60,
    check_returncode=True
)
print(f"Output: {stdout}")
```

##### copy_file_to_remote()

Copy file from local to remote host via SCP.

```python
def copy_file_to_remote(
    self,
    local_path: str,
    remote_path: str,
    timeout: int | None = None
) -> None
```

**Parameters**:
- `local_path`: Local file path (absolute)
- `remote_path`: Remote destination path
- `timeout`: Transfer timeout in seconds

**Raises**:
- `SSHFileTransferError`: File transfer failed

**Example**:
```python
client.copy_file_to_remote(
    local_path="/tmp/data.json",
    remote_path="/app/tmp/data.json",
    timeout=120
)
```

##### copy_file_from_remote()

Copy file from remote host to local via SCP.

```python
def copy_file_from_remote(
    self,
    remote_path: str,
    local_path: str,
    timeout: int | None = None
) -> None
```

**Parameters**:
- `remote_path`: Remote file path
- `local_path`: Local destination path (absolute)
- `timeout`: Transfer timeout in seconds

**Raises**:
- `SSHFileTransferError`: File transfer failed

**Example**:
```python
client.copy_file_from_remote(
    remote_path="/app/tmp/results.json",
    local_path="/tmp/results.json",
    timeout=120
)
```

##### check_remote_file_exists()

Check if file exists on remote host.

```python
def check_remote_file_exists(self, remote_path: str) -> bool
```

**Returns**: `True` if file exists, `False` otherwise

**Example**:
```python
if client.check_remote_file_exists("/app/config.yaml"):
    logger.info("Config file found")
```

##### get_remote_file_size()

Get size of file on remote host.

```python
def get_remote_file_size(self, remote_path: str) -> int
```

**Returns**: File size in bytes

**Raises**:
- `SSHCommandError`: File not found or stat failed

**Example**:
```python
size = client.get_remote_file_size("/app/data.json")
logger.info(f"File size: {size} bytes")
```

##### close()

Close SSH connection and cleanup resources.

```python
def close(self) -> None
```

**Example**:
```python
client.close()
```

#### Exception Classes

```python
class SSHConnectionError(Exception):
    """Raised when SSH connection fails."""

class SSHCommandError(Exception):
    """Raised when SSH command execution fails."""

class SSHFileTransferError(Exception):
    """Raised when file transfer fails."""
```

---

## DockerClient

Container operations layer, uses SSHClient for remote Docker commands.

**Location**: `src/clients/docker_client.py`

### Class: DockerClient

#### Constructor

```python
def __init__(
    self,
    ssh_client: SSHClient,
    container_name: str,
    timeout: int = 300
)
```

**Parameters**:
- `ssh_client`: Configured SSHClient instance
- `container_name`: Docker container name
- `timeout`: Default operation timeout in seconds

**Example**:
```python
from src.clients.docker_client import DockerClient

docker = DockerClient(
    ssh_client=ssh_client,
    container_name="openproject-web-1",
    timeout=300
)
```

#### Key Methods

##### execute_in_container()

Execute command inside Docker container.

```python
def execute_in_container(
    self,
    command: str,
    user: str | None = None,
    workdir: str | None = None,
    env: dict[str, str] | None = None
) -> tuple[int, str, str]
```

**Parameters**:
- `command`: Command to execute
- `user`: User to execute as (optional)
- `workdir`: Working directory (optional)
- `env`: Environment variables dict (optional)

**Returns**: Tuple of (return_code, stdout, stderr)

**Example**:
```python
returncode, stdout, stderr = docker.execute_in_container(
    command="bundle exec rails console",
    user="openproject",
    workdir="/app",
    env={"RAILS_ENV": "production"}
)
```

##### copy_file_to_container()

Copy file into container.

```python
def copy_file_to_container(
    self,
    local_path: str,
    container_path: str
) -> None
```

**Example**:
```python
docker.copy_file_to_container(
    local_path="/tmp/data.json",
    container_path="/app/tmp/data.json"
)
```

##### copy_file_from_container()

Copy file from container.

```python
def copy_file_from_container(
    self,
    container_path: str,
    local_path: str
) -> None
```

**Example**:
```python
docker.copy_file_from_container(
    container_path="/app/tmp/results.json",
    local_path="/tmp/results.json"
)
```

---

## RailsConsoleClient

Rails console interaction layer via tmux session.

**Location**: `src/clients/rails_console_client.py`

### Class: RailsConsoleClient

#### Constructor

```python
def __init__(
    self,
    ssh_client: SSHClient,
    docker_client: DockerClient,
    tmux_session_name: str = "rails_console",
    console_ready_timeout: int = 60,
    command_timeout: int = 300
)
```

**Parameters**:
- `ssh_client`: Configured SSHClient instance
- `docker_client`: Configured DockerClient instance
- `tmux_session_name`: tmux session name (default: "rails_console")
- `console_ready_timeout`: Timeout for console readiness (seconds)
- `command_timeout`: Default command timeout (seconds)

**Example**:
```python
from src.clients.rails_console_client import RailsConsoleClient

rails = RailsConsoleClient(
    ssh_client=ssh_client,
    docker_client=docker_client,
    tmux_session_name="rails_console",
    command_timeout=300
)
```

#### Key Methods

##### start_session()

Start tmux Rails console session.

```python
def start_session(self) -> None
```

**Raises**:
- `TmuxSessionError`: Failed to start session

**Example**:
```python
rails.start_session()
```

##### execute_ruby_code()

Execute Ruby code in Rails console.

```python
def execute_ruby_code(
    self,
    ruby_code: str,
    timeout: int | None = None
) -> str
```

**Parameters**:
- `ruby_code`: Ruby code to execute
- `timeout`: Execution timeout in seconds

**Returns**: Console output

**Raises**:
- `CommandExecutionError`: Execution failed
- `RubyError`: Ruby exception occurred

**Example**:
```python
ruby_code = """
User.where(admin: true).count
"""
result = rails.execute_ruby_code(ruby_code, timeout=60)
print(f"Admin count: {result}")
```

##### execute_ruby_script_with_file_result()

Execute Ruby script with file-based result retrieval.

```python
def execute_ruby_script_with_file_result(
    self,
    ruby_script: str,
    result_file_path: str,
    timeout: int | None = None
) -> dict[str, Any]
```

**Parameters**:
- `ruby_script`: Ruby script to execute
- `result_file_path`: Container path for results JSON
- `timeout`: Execution timeout in seconds

**Returns**: Parsed JSON result

**Raises**:
- `CommandExecutionError`: Execution failed
- `JsonParseError`: Failed to parse results

**Example**:
```python
script = f"""
results_file = '{result_path}'
data = JSON.parse(File.read('{data_path}'))
results = []
data.each do |item|
  user = User.create!(item)
  results << {{id: user.id, status: 'created'}}
end
File.write(results_file, JSON.pretty_generate(results))
"""
results = rails.execute_ruby_script_with_file_result(
    ruby_script=script,
    result_file_path="/app/tmp/results.json",
    timeout=600
)
```

##### stop_session()

Stop tmux Rails console session.

```python
def stop_session(self) -> None
```

**Example**:
```python
rails.stop_session()
```

#### Exception Classes

```python
class RailsConsoleError(Exception):
    """Base Rails console error."""

class TmuxSessionError(RailsConsoleError):
    """tmux session error."""

class ConsoleNotReadyError(RailsConsoleError):
    """Console not ready for commands."""

class CommandExecutionError(RailsConsoleError):
    """Command execution failed."""

class RubyError(RailsConsoleError):
    """Ruby exception occurred."""
```

---

## OpenProjectClient

High-level orchestration layer coordinating all clients.

**Location**: `src/clients/openproject_client.py`

### Class: OpenProjectClient

#### Constructor

```python
def __init__(
    self,
    ssh_connection: SSHConnection,
    batch_size: int = 100,
    timeout: int = 300
)
```

**Parameters**:
- `ssh_connection`: SSH connection configuration object
- `batch_size`: Default batch size for operations
- `timeout`: Default operation timeout

**Example**:
```python
from src.clients.openproject_client import OpenProjectClient, SSHConnection

conn = SSHConnection(
    host="openproject.example.com",
    user="admin",
    key_file="/path/to/key",
    container="openproject-web-1"
)

op_client = OpenProjectClient(
    ssh_connection=conn,
    batch_size=100,
    timeout=300
)
```

#### Key Methods

##### create_work_packages_batch()

Create work packages in batch via Rails console.

```python
def create_work_packages_batch(
    self,
    work_packages: list[dict[str, Any]],
    timeout: int | None = None
) -> list[dict[str, Any]]
```

**Parameters**:
- `work_packages`: List of work package dictionaries (sanitized)
- `timeout`: Batch operation timeout

**Returns**: List of created work package results with IDs

**Raises**:
- `QueryExecutionError`: Batch creation failed

**Example**:
```python
work_packages = [
    {
        "project_id": 1,
        "type_id": 2,
        "subject": "Migration task",
        "description": "Migrated from Jira"
    }
]
results = op_client.create_work_packages_batch(work_packages, timeout=600)
for result in results:
    print(f"Created WP: {result['id']}")
```

##### create_users_batch()

Create users in batch via Rails console.

```python
def create_users_batch(
    self,
    users: list[dict[str, Any]],
    timeout: int | None = None
) -> list[dict[str, Any]]
```

**Parameters**:
- `users`: List of user dictionaries (sanitized)
- `timeout`: Batch operation timeout

**Returns**: List of created user results with IDs

**Example**:
```python
users = [
    {
        "login": "user1",
        "firstname": "John",
        "lastname": "Doe",
        "mail": "john@example.com"
    }
]
results = op_client.create_users_batch(users, timeout=300)
```

##### create_projects_batch()

Create projects in batch via Rails console.

```python
def create_projects_batch(
    self,
    projects: list[dict[str, Any]],
    timeout: int | None = None
) -> list[dict[str, Any]]
```

**Parameters**:
- `projects`: List of project dictionaries (sanitized)
- `timeout`: Batch operation timeout

**Returns**: List of created project results with IDs

**Example**:
```python
projects = [
    {
        "identifier": "proj1",
        "name": "Project 1",
        "description": "Migrated project"
    }
]
results = op_client.create_projects_batch(projects, timeout=300)
```

#### Exception Classes

```python
class OpenProjectError(Exception):
    """Base OpenProject client error."""

class ConnectionError(OpenProjectError):
    """API connection error."""

class QueryExecutionError(OpenProjectError):
    """Query execution error."""

class RecordNotFoundError(OpenProjectError):
    """Record not found."""

class JsonParseError(OpenProjectError):
    """JSON parsing error."""

class FileTransferError(OpenProjectError):
    """File transfer error."""
```

---

## Usage Patterns

### Basic Client Initialization

```python
from src.clients.ssh_client import SSHClient
from src.clients.docker_client import DockerClient
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.openproject_client import OpenProjectClient, SSHConnection

# Method 1: Manual initialization
ssh_client = SSHClient(host="server", user="admin", key_file="/path/to/key")
ssh_client.connect()

docker_client = DockerClient(ssh_client, container_name="openproject-web-1")
rails_client = RailsConsoleClient(ssh_client, docker_client)
rails_client.start_session()

# Method 2: OpenProjectClient (recommended)
conn = SSHConnection(
    host="server",
    user="admin",
    key_file="/path/to/key",
    container="openproject-web-1"
)
op_client = OpenProjectClient(ssh_connection=conn)
```

### Error Handling Pattern

```python
from src.clients.exceptions import SSHCommandError, RailsConsoleError

try:
    result = op_client.create_work_packages_batch(work_packages)
except SSHCommandError as e:
    logger.error("SSH command failed", error=str(e), diagnostics=e.diagnostics)
    raise
except RailsConsoleError as e:
    logger.error("Rails console error", error=str(e), ruby_error=e.ruby_error)
    raise
except OpenProjectError as e:
    logger.error("OpenProject operation failed", error=str(e))
    raise
```

### Resource Cleanup

```python
try:
    # Perform operations
    results = op_client.create_users_batch(users)
finally:
    # Cleanup resources
    rails_client.stop_session()
    ssh_client.close()
```

---

## Configuration

### Environment Variables

```bash
# SSH Configuration
J2O_OPENPROJECT_SERVER=openproject.example.com
J2O_OPENPROJECT_USER=admin
J2O_OPENPROJECT_CONTAINER=openproject-web-1
J2O_OPENPROJECT_TMUX_SESSION_NAME=rails_console

# Timeouts
J2O_SSH_CONNECT_TIMEOUT=30
J2O_SSH_OPERATION_TIMEOUT=300
J2O_RAILS_CONSOLE_TIMEOUT=600
```

### Connection Testing

```bash
# Test SSH connectivity
ssh -i /path/to/key admin@openproject.example.com "echo test"

# Test Docker access
ssh admin@openproject.example.com "docker ps"

# Test tmux session
make start-rails ATTACH=true
```

---

## Performance Considerations

### Connection Pooling
- SSHClient maintains persistent connections
- Auto-reconnect on connection loss
- Connection reuse across operations

### Timeout Configuration
- `connect_timeout`: Initial connection establishment
- `operation_timeout`: General operation timeout
- `command_timeout`: Rails console command timeout

### Batch Operations
- Use batch methods for multiple records
- Recommended batch size: 50-100 records
- Monitor memory usage for large batches

---

## Security Best Practices

### SSH Key Management
- Use key-based authentication (prefer over passwords)
- Set proper key file permissions: `chmod 600 /path/to/key`
- Rotate keys regularly

### Input Validation
- Always validate Jira keys before use
- Sanitize JSON payloads before Rails execution
- Remove `_links` objects from OpenProject API responses

### Error Information
- Avoid logging sensitive data in error messages
- Redact credentials from logs
- Use diagnostics dict for detailed error info

---

## Related Documentation

- **[Architecture Overview](ARCHITECTURE.md)**: System architecture and component design
- **[Migration Components](MIGRATION_COMPONENTS.md)**: Migration module reference
- **[Security Guidelines](SECURITY.md)**: Security best practices
- **[Developer Guide](DEVELOPER_GUIDE.md)**: Development standards

---

## Support

For issues or questions:
1. Check [Troubleshooting](#troubleshooting) section
2. Review logs in `var/logs/`
3. Consult [Developer Guide](DEVELOPER_GUIDE.md)
4. Open issue in repository
