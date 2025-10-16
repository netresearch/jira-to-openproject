# Security

## Credential Management

**Critical**: Never commit credentials to version control.

All sensitive data must be provided via environment variables or secure configuration files that are git-ignored:

- `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`
- `OPENPROJECT_URL`, `OPENPROJECT_API_KEY`
- SSH keys and Docker credentials

## Data Protection

The migration tool processes sensitive project data. Ensure:

1. **Access Control**: Limit tool execution to authorized personnel only
2. **Data in Transit**: Use HTTPS/TLS for all API communications
3. **Data at Rest**: Temp files contain sensitive data - secure the container environment
4. **Audit Logging**: All migration operations are logged for security auditing

## Container Security

The tool runs in Docker containers. Follow container security best practices:

- Use official base images
- Keep Docker images updated
- Limit container privileges
- Scan images for vulnerabilities
- Isolate container networks

## API Key Security

Both Jira and OpenProject require API authentication:

- **Jira**: Use API tokens, never passwords
- **OpenProject**: Use API keys with minimum required permissions
- Rotate credentials regularly
- Revoke unused credentials immediately

## Vulnerability Reporting

If you discover a security vulnerability:

1. **Do not** open a public GitHub issue
2. Contact the project maintainers directly
3. Provide detailed information about the vulnerability
4. Allow reasonable time for patching before disclosure

## Security Updates

- Monitor dependency security advisories
- Run `uv pip check` regularly to identify vulnerable dependencies
- Apply security patches promptly
- Review `dependabot` alerts if enabled

## Secure Development Practices

See [`DEVELOPER_GUIDE.md`](./DEVELOPER_GUIDE.md) for security requirements including:

- Exception-based error handling (no silent failures)
- Input validation and sanitization
- Secure temp file handling
- SSH command injection prevention
- Secrets scanning in CI/CD
