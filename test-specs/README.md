# Test Specifications Directory

This directory contains OpenAPI specifications for mock API services used during development and testing.

## Overview

The mock services are optional components that provide fake API endpoints for:
- **Jira API** (port 4010) - Mock Atlassian Jira API
- **OpenProject API** (port 4011) - Mock OpenProject API

## Purpose

These mock services enable:
- **Integration Testing** - Test API integrations without external dependencies
- **Development** - Work on API clients without requiring live services
- **Offline Development** - Continue development without internet connectivity

## Current Status

⚠️ **PLACEHOLDER IMPLEMENTATION**

The current OpenAPI specifications are **minimal placeholders** created to:
- Prevent Docker mount failures during development
- Provide basic mock endpoints for future use
- Establish infrastructure for testing

## Files

- `jira-openapi.yml` - Minimal Jira API specification
- `openproject-openapi.yml` - Minimal OpenProject API specification
- `README.md` - This documentation file

## Usage

### Starting Mock Services

```bash
# Start testing profile (includes mock services)
make dev-testing

# Start everything (dev + services + testing)
make dev-full
```

### Accessing Mock APIs

- **Jira Mock API**: http://localhost:4010
- **OpenProject Mock API**: http://localhost:4011

### API Documentation

When running, you can view interactive API documentation:
- Jira Mock: http://localhost:4010/__prism/
- OpenProject Mock: http://localhost:4011/__prism/

## Security Configuration

The mock services are configured with security best practices for development environments:

### Network Security
- **Localhost-only binding**: Services are only accessible from the development machine (127.0.0.1)
- **No external network exposure**: Port mapping prevents access from other machines on the network
- **Container isolation**: Services run in isolated Docker containers with proper network segmentation

### Development Safety
- **Mock data only**: Services contain no real production data
- **No authentication required**: Simplified for development workflows
- **Resource isolation**: Services are isolated from production systems

### Configuration Details
The security is implemented through Docker Compose port mapping:
```yaml
ports:
  - "127.0.0.1:4010:4010"  # Localhost-only access
  - "127.0.0.1:4011:4011"  # Localhost-only access
```

This ensures mock services cannot be accessed from external networks while maintaining full functionality for local development and testing.

## Development Notes

### Mock Service Technology

The mock services use [Stoplight Prism](https://github.com/stoplightio/prism) which:
- Generates mock responses from OpenAPI specifications
- Provides realistic example data
- Validates request/response schemas
- Supports dynamic response examples

### Current Limitations

1. **Minimal Endpoints** - Only basic endpoints are defined
2. **Placeholder Data** - Responses use static example data
3. **No Authentication** - Mock services don't implement auth flows
4. **Limited Validation** - Basic schema validation only

### Future Improvements

To make these mock services more useful for development:

1. **Expand API Coverage**
   - Add more endpoints based on actual migration tool needs
   - Include error scenarios and edge cases
   - Add realistic data relationships

2. **Dynamic Responses**
   - Implement stateful mock responses
   - Add request/response correlation
   - Support CRUD operations with persistence

3. **Authentication Mock**
   - Add OAuth/JWT token flows
   - Implement role-based access control
   - Mock various auth scenarios

4. **Integration Tests**
   - Create test suites that use these mock services
   - Add contract testing between real and mock APIs
   - Implement automated API validation

## Configuration

Mock services are defined in `compose.yml` under the `testing` profile:

```yaml
# Mock Jira API server for testing
mock-jira:
  image: stoplight/prism:4
  command: mock -h 0.0.0.0 -p 4010 /specs/jira-openapi.yml
  ports:
    - "4010:4010"
  volumes:
    - ./test-specs:/specs:ro

# Mock OpenProject API server for testing
mock-openproject:
  image: stoplight/prism:4
  command: mock -h 0.0.0.0 -p 4011 /specs/openproject-openapi.yml
  ports:
    - "4011:4011"
  volumes:
    - ./test-specs:/specs:ro
```

## Maintenance

When updating these specifications:

1. **Validate Syntax** - Ensure OpenAPI specs are valid YAML
2. **Test Mock Services** - Start services and verify endpoints work
3. **Update Documentation** - Keep this README in sync with changes
4. **Version Control** - Commit specification changes with clear messages

## Resources

- [OpenAPI Specification](https://swagger.io/specification/)
- [Stoplight Prism Documentation](https://meta.stoplight.io/docs/prism/)
- [Jira API Documentation](https://developer.atlassian.com/cloud/jira/platform/rest/v3/)
- [OpenProject API Documentation](https://www.openproject.org/docs/api/)
