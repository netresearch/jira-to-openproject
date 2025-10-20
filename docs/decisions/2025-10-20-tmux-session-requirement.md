# ADR: Tmux Session Requirement for Rails Console

**Date**: 2025-10-20
**Status**: Accepted
**Deciders**: Development Team
**Context**: j2o migration tool Rails console execution architecture

## Context and Problem Statement

The j2o migration tool requires Rails console access to OpenProject for bulk data operations (see [2025-10-20: Rails Console Requirement](2025-10-20-rails-console-requirement.md)). We must decide how to execute Rails console commands: one-off sessions, rails runner, or persistent tmux session.

## Decision Drivers

- **Performance**: Minimize connection overhead for thousands of operations
- **Reliability**: Stable execution environment for long-running operations
- **Session State**: Maintain console state across operations
- **Error Recovery**: Ability to inspect and recover from failures
- **Monitoring**: Capability to observe and debug live operations

## Considered Options

1. **One-off Rails Console Sessions** - Start new `rails console` for each operation
2. **Rails Runner** - Use `bundle exec rails runner` for script execution
3. **Persistent Tmux Session** - Long-lived Rails console in tmux session
4. **Hybrid Approach** - Mix of tmux (primary) and rails runner (fallback)

## Decision Outcome

**Chosen**: Persistent Tmux Session (Option 3) with Rails Runner fallback (Option 4)

**Rationale**: Persistent tmux session provides optimal performance, reliability, and operational visibility for bulk migration workloads.

### Performance Analysis

#### Persistent Tmux Session (Chosen)

**Startup Cost**: Once per migration (5-10 seconds)
**Per-operation Cost**: <100ms (command transmission only)
**1000 operations**: ~1.7 minutes (100ms × 1000)

**Evidence from codebase**:
```python
# From openproject_client.py:917
# Execute via persistent tmux Rails console (faster than rails runner)
```

#### One-off Rails Console

**Startup Cost**: Per operation (5-10 seconds each)
**Per-operation Cost**: 5-10 seconds (full Rails boot)
**1000 operations**: 83-167 minutes (5-10s × 1000)

**Overhead**: 50-100x slower than persistent session

#### Rails Runner

**Startup Cost**: Per operation (3-5 seconds each)
**Per-operation Cost**: 3-5 seconds (Rails boot + execute + exit)
**1000 operations**: 50-83 minutes (3-5s × 1000)

**Overhead**: 30-50x slower than persistent session

**Used as fallback**:
```python
# From openproject_client.py:562
"Rails console execution failed (%s). Falling back to rails runner."
```

### Architectural Integration

#### Client Layer Architecture

**From CLIENT_API.md and ARCHITECTURE.md**:
```
OpenProjectClient (Orchestration Layer)
    ├── SSHClient (Foundation Layer)
    ├── DockerClient (Container Interaction Layer)
    └── RailsConsoleClient (Console Interaction Layer)
```

**RailsConsoleClient** is a first-class architectural component, not optional:
- Dedicated layer for tmux console interaction
- Session lifecycle management (start_session, stop_session)
- Command execution with timeout and error handling
- File-based result retrieval

#### Initialization Pattern

**From migration.py:545-550, 577-590**:
```python
rails_client = RailsConsoleClient(
    tmux_session_name=config.openproject_config.get(
        "tmux_session_name",
        "rails_console",
    ),
)

op_client = OpenProjectClient(
    # ... other clients ...
    rails_client=rails_client,  # Always required
    # ...
)
```

RailsConsoleClient is **unconditionally initialized** - not optional or conditional.

### Tmux Session Benefits

#### 1. Session Persistence

**Maintains state across operations**:
- Console environment loaded once
- Active database connections pooled
- Rails application context preserved
- Memory allocations reused

#### 2. Performance Optimization

**From ARCHITECTURE.md:189**:
```markdown
- **Session reuse** for tmux console interactions
```

Benefits:
- Eliminates repeated Rails boot overhead
- Connection pooling across operations
- Memory efficiency from process reuse
- CPU efficiency from warmed-up JIT/caches

#### 3. Operational Visibility

**Live monitoring capability**:
```bash
# Attach to running session for inspection
make attach-rails

# View console output in real-time
tmux attach -t rails_console
```

**Logging support** (from AGENTS.md:208):
```bash
pipe-pane -o 'cat >>~/tmux.log'
```

All console activity captured to log file for audit and debugging.

#### 4. Error Recovery

**Session survives network interruptions**:
- Tmux session persists on remote host
- Operations continue despite SSH disconnection
- Reconnect and resume monitoring
- Inspect session state after failures

#### 5. Stability Configuration

**IRB Configuration for Non-Interactive Use** (from AGENTS.md:186-194):
```markdown
To stabilize the tmux-backed Rails console session used by RailsConsoleClient,
install an `.irbrc` into the OpenProject container. This disables multiline
and relines interactive features which can break non-interactive execution flows:

- Source file: contrib/openproject.irbrc
- Install command: make install-irbrc
- Destination in container: /app/.irbrc
```

Stability features:
- Disable interactive prompts and multi-line mode
- Prevent readline interference with output parsing
- Ensure consistent output format for parsing
- Avoid escape sequences that break automation

### Fallback Strategy

**Hybrid Approach** (from codebase analysis):

The architecture uses **persistent tmux as primary** with **rails runner as fallback**:

```python
# Pattern found throughout openproject_client.py:
try:
    # 1. Try persistent tmux console (fastest)
    result = rails_client.execute_ruby_code(script)
except ConsoleError:
    # 2. Fall back to rails runner (slower but more reliable)
    result = docker_client.execute_in_container(
        f"bundle exec rails runner {script_path}"
    )
```

**Fallback triggers**:
- Console not ready or session died
- Command timeout in console
- Parsing errors from console output
- Explicit configuration to prefer rails runner

### Configuration and Setup

#### Environment Variables

```bash
# From CLIENT_API.md:790
J2O_OPENPROJECT_TMUX_SESSION_NAME=rails_console
```

#### Session Management

**Start session** (from AGENTS.md:200):
```bash
make start-rails ATTACH=true  # Start and attach
make start-rails              # Start only
make attach-rails             # Attach to running session
```

**Session command** (from AGENTS.md:206-210):
```bash
tmux new-session -s rails_console \; \
  pipe-pane -o 'cat >>~/tmux.log' \; \
  send-keys 'ssh -t $J2O_OPENPROJECT_USER@$J2O_OPENPROJECT_SERVER \
    "docker exec -e IRBRC=/app/.irbrc -ti $J2O_OPENPROJECT_CONTAINER \
    bundle exec rails console"' C-m
```

#### Pre-Migration Checklist

**From QUICK_START.md:81-97**:
1. Install tmux on host if not present
2. Install `.irbrc` to container: `make install-irbrc`
3. Start Rails console session: `make start-rails`
4. Verify session exists: `tmux list-sessions | grep rails_console`

### Exception Handling

**From CLIENT_API.md:536-552**:
```python
class RailsConsoleError(Exception):
    """Base Rails console error."""

class TmuxSessionError(RailsConsoleError):
    """tmux session error."""

class ConsoleNotReadyError(RailsConsoleError):
    """Console not ready for commands."""

class CommandExecutionError(RailsConsoleError):
    """Command execution failed."""
```

Dedicated exception hierarchy for tmux session management.

## Consequences

### Positive

- **Performance**: 50-100x faster than one-off sessions or rails runner
- **Efficiency**: Single Rails boot for entire migration (minutes vs hours)
- **Reliability**: Persistent session survives network interruptions
- **Observability**: Live monitoring and logging of all operations
- **Debugging**: Attach to session for real-time inspection
- **State Management**: Maintains console context and connections
- **Recovery**: Can resume after failures without restarting

### Negative

- **Setup Complexity**: Requires tmux installation and session management
- **Session Lifecycle**: Must start session before migration
- **Resource Usage**: Persistent process consumes memory throughout migration
- **Cleanup Required**: Must stop session after migration completes
- **Host Dependency**: Tmux must be available on migration host (not container)

### Risk Mitigation

- **Fallback Strategy**: Rails runner as backup for console failures
- **Health Checks**: Verify session exists before migration starts
- **Timeout Protection**: Command-level timeouts prevent hung operations
- **Logging**: Complete audit trail via tmux pipe-pane
- **Error Handling**: Comprehensive exception hierarchy for failure scenarios
- **Documentation**: Clear setup instructions in QUICK_START.md

## Implementation Details

### RailsConsoleClient Interface

**From CLIENT_API.md:432-532**:

```python
class RailsConsoleClient:
    def start_session(self) -> None:
        """Start tmux Rails console session."""

    def execute_ruby_code(
        self,
        ruby_code: str,
        timeout: int | None = None
    ) -> str:
        """Execute Ruby code in Rails console."""

    def execute_ruby_script_with_file_result(
        self,
        ruby_script: str,
        result_file_path: str,
        timeout: int | None = None
    ) -> dict[str, Any]:
        """Execute Ruby script with file-based result retrieval."""

    def stop_session(self) -> None:
        """Stop tmux Rails console session."""
```

### Workflow Pattern

**Standard operation flow**:
```python
# 1. Initialize clients (migration.py)
rails_client = RailsConsoleClient(tmux_session_name="rails_console")

# 2. Start session (once per migration)
rails_client.start_session()

# 3. Execute operations (reuses session)
for batch in batches:
    result = rails_client.execute_ruby_script_with_file_result(
        ruby_script=script,
        result_file_path="/app/tmp/results.json"
    )

# 4. Cleanup (end of migration)
rails_client.stop_session()
```

## Related Decisions

- [2025-10-20: Rails Console Requirement](2025-10-20-rails-console-requirement.md) - Why Rails console is required
- [AGENTS.md](../../AGENTS.md) - Rails Console tmux Session (lines 186-210)
- [CLIENT_API.md](../CLIENT_API.md) - RailsConsoleClient reference
- [ARCHITECTURE.md](../ARCHITECTURE.md) - Client layer architecture

## References

- `src/clients/rails_console_client.py` - Tmux session management implementation
- `src/clients/openproject_client.py` - Fallback strategy implementation
- `src/migration.py` - Client initialization and lifecycle
- `docs/ARCHITECTURE.md` - Architecture overview
- `docs/CLIENT_API.md` - Client API reference
- `docs/QUICK_START.md` - Session setup instructions
- `contrib/openproject.irbrc` - IRB stability configuration
- `AGENTS.md` - Rails console tmux session setup

## Conclusion

**A persistent tmux session is required** for Rails console access during migration operations. This is not optional - it is an **architectural requirement** driven by:

1. **Performance**: 50-100x speedup vs alternatives
2. **Architecture**: RailsConsoleClient is a first-class component
3. **Reliability**: Session persistence and error recovery
4. **Observability**: Live monitoring and complete audit trail

The migration tool is designed around this pattern and cannot function efficiently without it.

### Pre-Migration Requirements

Before running any migration, operators **MUST**:
1. Ensure tmux is installed on migration host
2. Install IRB configuration: `make install-irbrc`
3. Start Rails console session: `make start-rails`
4. Verify session exists: `tmux list-sessions | grep rails_console`

Failure to establish tmux session will result in:
```
TmuxSessionError: tmux session 'rails_console' does not exist
```

### Architectural Rule

**Rails console operations require persistent tmux session.** This is a fundamental architectural constraint that enables the performance and reliability characteristics required for bulk migration operations.

Any alternative approach (one-off sessions, rails runner only) would result in 50-100x performance degradation and is **not architecturally supported**.
