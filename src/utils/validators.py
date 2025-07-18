#!/usr/bin/env python3
"""Shared validation utilities for preventing injection attacks.

This module provides centralized validation functions used across the migration
system to ensure consistent security controls and reduce code duplication.
"""

import re


def validate_jira_key(jira_key: str) -> None:
    """
    Validate JIRA key format to prevent injection attacks.
    
    This function enforces strict validation rules to prevent SQL injection,
    script injection, and other security vulnerabilities when JIRA keys are
    used in script generation or database operations.
    
    Security Rules:
    - Non-empty alphanumeric strings with hyphens only
    - Pattern: ^[A-Z0-9\\-]+$
    - Maximum 100 characters
    - No control characters (ASCII < 32)
    
    Valid Examples:
    - "PROJ-123"
    - "TEST-456"
    - "TEAM-999"
    - "A-1"
    - "PROJECT-LONGNAME-123"
    
    Invalid Examples:
    - "proj-123" (lowercase)
    - "TEST'; DROP TABLE users;" (SQL injection)
    - "PROJ<script>alert(1)</script>" (script injection)
    - "TEST\\nBAD" (control characters)
    - "PROJ_123" (underscore not allowed)
    
    Args:
        jira_key: The JIRA key to validate
        
    Raises:
        ValueError: If jira_key format is invalid or contains potentially dangerous characters
        
    Note:
        This validator is used by both EnhancedUserAssociationMigrator and
        EnhancedTimestampMigrator to ensure consistent security controls.
    """
    if not jira_key or not jira_key.strip():
        raise ValueError("JIRA key cannot be empty or whitespace only.")
    
    if len(jira_key) > 100:
        raise ValueError(f"JIRA key too long ({len(jira_key)} chars). Maximum allowed: 100 characters.")
    
    # Check for control characters (ASCII < 32)
    for char in jira_key:
        if ord(char) < 32:
            raise ValueError(f"JIRA key contains control characters (ASCII {ord(char)}). Only A-Z, 0-9, and hyphens allowed.")
    
    # Validate against regex pattern
    if not re.match(r'^[A-Z0-9\-]+$', jira_key):
        raise ValueError(f"Invalid jira_key format: {jira_key}. Must contain only A-Z, 0-9, and hyphens.") 