# Security Documentation - Jira to OpenProject Migration

## Overview

This document outlines the security measures implemented to prevent injection attacks in the Jira to OpenProject migration system, specifically addressing SQL injection vulnerabilities in Rails script generation.

## Vulnerability Description

### Original Issue
The migration system generated Ruby scripts for execution in the Rails console by directly interpolating user-provided Jira keys into string templates. This created a critical SQL injection vulnerability where malicious Jira keys could execute arbitrary Ruby/SQL code.

**Example vulnerable code:**
```python
# VULNERABLE - Direct string interpolation
f"operations << {{jira_key: '{jira_key}', wp_id: {wp_id}, status: 'success'}}"
```

**Attack vector:**
```
jira_key = "TEST'; User.destroy_all; puts 'pwned"
# Results in: operations << {jira_key: 'TEST'; User.destroy_all; puts 'pwned', wp_id: 123, status: 'success'}
```

## Security Fixes Implemented

### 1. Input Validation Layer

**Files:** 
- `src/utils/enhanced_user_association_migrator.py`
- `src/utils/enhanced_timestamp_migrator.py`

**Method:** `_validate_jira_key(jira_key: str)`

**Validation Rules:**
1. **Non-empty validation:** Rejects empty strings and None values
2. **Character whitelist:** Only allows `A-Z`, `0-9`, and `-` characters
3. **Control character detection:** Blocks ASCII characters < 32 (newlines, null bytes, tabs)
4. **Length limits:** Maximum 100 characters to prevent buffer overflow

**Regex pattern:** `^[A-Z0-9\-]+$`

**Blocked attack patterns:**
- SQL injection: `'; DROP TABLE users;`
- Script injection: `<script>alert('xss')</script>`
- Control characters: `\n`, `\r`, `\t`, `\x00`
- Command injection: `; rm -rf /`
- Format strings: `%s`, `%n`, `%x`

### 2. Safe Output Escaping

**Implementation:** All validated jira_key values are escaped using `json.dumps()` before inclusion in Ruby scripts.

**Before (vulnerable):**
```python
f"{{jira_key: '{jira_key}', wp_id: {wp_id}}}"
```

**After (secure):**
```python
escaped_jira_key = json.dumps(jira_key)
f"{{jira_key: {escaped_jira_key}, wp_id: {wp_id}}}"
```

**Security benefit:** `json.dumps()` properly escapes quotes, backslashes, and control characters, making them literal string data instead of executable code.

### 3. Defense in Depth

**Multiple security layers:**
1. **Input validation** at the entry point (when jira_key is first used)
2. **Output escaping** during script generation  
3. **Error isolation** with begin/rescue blocks in generated Ruby
4. **Parameterized queries** for database operations (`WorkPackage.find(id)`)

## Security Testing

### Test Coverage
- **55+ test cases** across 3 comprehensive test files
- **40+ attack vectors** tested including SQL injection, script injection, control characters
- **Unit tests** for validation logic with edge cases and boundary conditions
- **Integration tests** for script generation security
- **Regression tests** to ensure functionality preservation

### Test Files
1. `tests/security/test_jira_key_validation.py` - Unit tests for validation
2. `tests/security/test_script_generation_security.py` - Integration security tests  
3. `tests/security/test_security_regression.py` - Regression and compatibility tests

### Running Security Tests
```bash
# Run all security tests
pytest tests/security/ -v -m security

# Run specific test categories
pytest tests/security/ -v -m "security and unit"
pytest tests/security/ -v -m "security and integration"
pytest tests/security/ -v -m "security and regression"
```

## Secure Development Guidelines

### For Developers

1. **Always validate external input** before using in script generation
2. **Use json.dumps() for escaping** user data in Ruby hash literals
3. **Never use direct string interpolation** with user-provided data
4. **Add security tests** for any new script generation functionality
5. **Review generated scripts** to ensure no unescaped user data appears

### Code Review Checklist

- [ ] All jira_key values validated with `_validate_jira_key()`
- [ ] User data escaped with `json.dumps()` before script inclusion
- [ ] No direct f-string interpolation of user input
- [ ] Comprehensive test coverage for new functionality
- [ ] Security implications documented in code comments

### Safe Patterns

```python
# ✅ SAFE - Validate then escape
self._validate_jira_key(jira_key)
escaped_key = json.dumps(jira_key)
script_line = f"{{jira_key: {escaped_key}, wp_id: {wp_id}}}"

# ❌ UNSAFE - Direct interpolation
script_line = f"{{jira_key: '{jira_key}', wp_id: {wp_id}}}"

# ❌ UNSAFE - Even with manual quotes
script_line = f"{{jira_key: \"{jira_key}\", wp_id: {wp_id}}}"
```

## Threat Model

### Attack Surfaces
1. **Jira API responses** - External data source requiring validation
2. **User configuration files** - Could contain malicious jira_key mappings
3. **Import/export functionality** - File-based data input

### Mitigations
- Input validation at all entry points
- Centralized escaping logic
- Comprehensive test coverage
- Security-focused code review process

### Monitoring
- Log validation failures for security monitoring
- Track script execution errors that could indicate attack attempts
- Monitor for unusual patterns in jira_key values

## Incident Response

### If Security Issue Detected
1. **Immediate:** Stop script execution and isolate affected systems
2. **Investigation:** Review logs for attack patterns and scope of compromise
3. **Remediation:** Apply security patches and update validation rules
4. **Prevention:** Add test cases for new attack vectors discovered

### Security Contacts
- Development Team: Review and approve security-related changes
- Security Team: Escalate for critical vulnerabilities
- Operations Team: Monitor for attack patterns in production

## Compliance and Auditing

### Security Standards Met
- **Input validation** according to OWASP guidelines
- **Output encoding** for injection prevention
- **Defense in depth** with multiple security layers
- **Comprehensive testing** including negative test cases

### Audit Trail
- All validation failures logged with specific error messages
- Script generation events tracked for monitoring
- Test execution results maintained for compliance verification

---

**Last Updated:** 2024-12-19
**Version:** 1.0
**Status:** Active - All security measures implemented and tested 