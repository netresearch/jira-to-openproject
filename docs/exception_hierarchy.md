# Exception Hierarchy in Client Architecture

This document illustrates the exception hierarchy implemented across the client architecture layers.

## SSHClient Exceptions

```
Exception
  └── SSHConnectionError (inherits from ConnectionError)
  └── SSHCommandError
  └── SSHFileTransferError
```

## DockerClient Exceptions

DockerClient primarily uses SSHClient exceptions since it delegates most operations to the SSHClient.

## RailsConsoleClient Exceptions

```
Exception
  └── RailsConsoleError
        ├── TmuxSessionError
        ├── ConsoleNotReadyError
        └── CommandExecutionError
              └── RubyError
```

## OpenProjectClient Exceptions

```
Exception
  └── OpenProjectError
        ├── ConnectionError
        ├── FileTransferError
        ├── QueryExecutionError
        ├── RecordNotFoundError
        └── JsonParseError
```

## JiraClient Exceptions

```
Exception
  └── JiraError
        ├── JiraConnectionError
        ├── JiraAuthenticationError
        ├── JiraApiError
        ├── JiraResourceNotFoundError
        └── JiraCaptchaError
```

## Exception Propagation

The layered architecture ensures that exceptions propagate upwards in a logical manner:

1. Low-level exceptions from SSHClient can propagate through DockerClient to OpenProjectClient
2. OpenProjectClient can catch these exceptions and translate them to appropriate high-level exceptions
3. Each layer can add context to exceptions as they propagate upwards
4. Top-level applications should catch OpenProjectError or JiraError types

## Best Practices for Exception Handling

When using this client architecture:

1. **Catch Specific Exceptions**: Start by catching specific exception types, then more general ones
2. **Preserve Context**: When re-raising exceptions, include the original exception information
3. **Add Useful Messages**: Include contextual information about what operation failed
4. **Handle at Appropriate Level**: Catch exceptions at the level where they can be meaningfully handled

## Example

```python
try:
    # Attempt to create a user in OpenProject
    user = op_client.create_record("User", {"login": "new_user", "mail": "user@example.com"})
except RecordNotFoundError as e:
    # Handle specific case - record not found
    print(f"Unable to find related record: {e}")
except QueryExecutionError as e:
    # Handle query execution problems
    print(f"Query execution failed: {e}")
except OpenProjectError as e:
    # Handle any other OpenProject-related errors
    print(f"OpenProject operation failed: {e}")
except Exception as e:
    # Handle unexpected errors
    print(f"Unexpected error: {e}")
```
