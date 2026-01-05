# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

We take security vulnerabilities seriously. If you discover a security issue, please report it responsibly.

### Preferred Method: GitHub Security Advisories

1. Go to the [Security tab](https://github.com/netresearch/jira-to-openproject/security/advisories) of this repository
2. Click "Report a vulnerability"
3. Fill out the form with details about the vulnerability

### Alternative: Email

If you prefer email, contact the maintainers directly. Please include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested fixes

### Response Timeline

- **Initial Response**: Within 48 hours
- **Status Update**: Within 7 days
- **Resolution Target**: Within 30 days for critical issues

### What to Expect

1. Acknowledgment of your report
2. Assessment of the vulnerability
3. Development of a fix
4. Coordinated disclosure (if applicable)
5. Credit in the release notes (unless you prefer anonymity)

## Security Measures

This project implements several security measures:

- **CodeQL Analysis**: Automated security scanning on every PR
- **Dependency Scanning**: Regular checks for vulnerable dependencies
- **Input Validation**: Strict validation of user inputs
- **Secrets Management**: No hardcoded credentials; environment-based configuration
- **Least Privilege**: Minimal permissions for CI/CD workflows

## Out of Scope

The following are not considered security vulnerabilities:

- Issues in dependencies that don't affect this project
- Theoretical vulnerabilities without proof of concept
- Social engineering attacks
- Physical security issues
