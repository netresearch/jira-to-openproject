# Migration Guide: Dictionary-Based to Exception-Based Error Handling

This guide helps developers transition their code from the old dictionary-based error handling approach to the new YOLO-style exception-based approach.

## Overview of Changes

The client architecture has been completely refactored to use exception-based error handling instead of returning status dictionaries. This provides:

- More predictable error handling with try/except blocks
- Cleaner code that doesn't require checking status keys
- Better IDE support with typed exceptions
- More detailed error information
- Proper propagation of errors through the stack

## Migration Steps

### 1. SSHClient Changes

#### Old Approach
```python
result = ssh_client.execute_command("ls -la")
if result["status"] == "success":
    print(result["stdout"])
else:
    print(f"Error: {result['error']}")
```

#### New Approach
```python
try:
    stdout, stderr, returncode = ssh_client.execute_command("ls -la")
    print(stdout)
except SSHCommandError as e:
    print(f"Error: {e}")
```

### 2. DockerClient Changes

#### Old Approach
```python
result = docker_client.execute_command("echo hello")
if result["status"] == "success":
    print(result["output"])
else:
    print(f"Docker error: {result['error']}")
```

#### New Approach
```python
try:
    stdout, stderr, returncode = docker_client.execute_command("echo hello")
    print(stdout)
except SSHCommandError as e:
    print(f"Docker error: {e}")
```

### 3. RailsConsoleClient Changes

#### Old Approach
```python
result = rails_client.execute("User.count")
if result["status"] == "success":
    user_count = result["output"]
else:
    print(f"Rails error: {result['error']}")
```

#### New Approach
```python
try:
    user_count = rails_client.execute("User.count")
    print(user_count)
except CommandExecutionError as e:
    print(f"Rails error: {e}")
```

### 4. OpenProjectClient Changes

#### Old Approach
```python
result = op_client.find_record("User", {"email": "admin@example.com"})
if result["status"] == "success":
    user = result["data"]
elif result["status"] == "not_found":
    print("User not found")
else:
    print(f"Error: {result['error']}")
```

#### New Approach
```python
try:
    user = op_client.find_record("User", {"email": "admin@example.com"})
    # User data returned directly
except RecordNotFoundError:
    print("User not found")
except OpenProjectError as e:
    print(f"Error: {e}")
```

### 5. JiraClient Changes

#### Old Approach
```python
result = jira_client.get_issue("PROJ-123")
if result["status"] == "success":
    issue = result["data"]
elif result["status"] == "not_found":
    print("Issue not found")
else:
    print(f"Error: {result['error']}")
```

#### New Approach
```python
try:
    issue = jira_client.get_issue("PROJ-123")
    # Issue data returned directly
except JiraResourceNotFoundError:
    print("Issue not found")
except JiraError as e:
    print(f"Error: {e}")
```

## Common Migration Patterns

### If/Else Chain Replacement

#### Old Approach
```python
result = client.some_operation()
if result["status"] == "success":
    # Handle success
elif result["status"] == "not_found":
    # Handle not found
elif result["status"] == "auth_error":
    # Handle auth error
else:
    # Handle other errors
```

#### New Approach
```python
try:
    result = client.some_operation()
    # Handle success
except ResourceNotFoundError:
    # Handle not found
except AuthenticationError:
    # Handle auth error
except OperationError as e:
    # Handle other errors
```

### Nested Error Handling

#### Old Approach
```python
result1 = client.operation1()
if result1["status"] == "success":
    result2 = client.operation2(result1["data"])
    if result2["status"] == "success":
        # Use result2["data"]
    else:
        # Handle operation2 error
else:
    # Handle operation1 error
```

#### New Approach
```python
try:
    result1 = client.operation1()
    result2 = client.operation2(result1)
    # Use result2 directly
except OperationError as e:
    # The error will tell you which operation failed
    print(f"Error: {e}")
```

## Best Practices

1. **Import Specific Exceptions**: Import the specific exceptions you need from each client module
2. **Catch Specific First**: Catch specific exception types first, then more general ones later
3. **Use Context Managers**: Consider `with` blocks where appropriate for resource management
4. **Create Helper Functions**: For common patterns, create helper functions that handle exceptions consistently

## Testing Your Migration

1. Use the provided tests as examples for how to test code with the new exception-based approach
2. Make sure to test both success and various error scenarios
3. Verify that error messages contain useful diagnostic information

## Conclusion

This migration significantly improves code quality and maintainability by:

- Simplifying control flow
- Removing repetitive status checking
- Providing more detailed error information
- Following Python's "Easier to ask for forgiveness than permission" (EAFP) philosophy
- Enabling better static analysis and IDE support
