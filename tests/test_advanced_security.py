#!/usr/bin/env python3
"""Tests for the Advanced Security Features."""
import asyncio
import json
import pytest
import tempfile
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.utils.advanced_security import (
    SecurityManager, SecurityConfig, EncryptionManager, CredentialManager,
    AuditLogger, AccessControlManager, RateLimiter, SecurityScanner,
    UserRole, SecurityLevel, AuditEventType, AuditEvent, UserSession,
    create_security_manager, secure_api_call, encrypt_config_value, decrypt_config_value
)


class TestSecurityConfig:
    def test_security_config_creation(self):
        config = SecurityConfig()
        assert config.max_login_attempts == 5
        assert config.lockout_duration == 300
        assert config.session_timeout == 3600
        assert config.password_min_length == 12
        assert config.enable_2fa is True
        assert config.enable_audit_logging is True


class TestEncryptionManager:
    def test_encryption_manager_creation(self, tmp_path):
        key_path = tmp_path / "keys"
        manager = EncryptionManager(key_path)
        
        assert key_path.exists()
        assert manager._fernet_key is not None
        assert manager._rsa_private_key is not None
        assert manager._rsa_public_key is not None
    
    def test_symmetric_encryption_decryption(self, tmp_path):
        key_path = tmp_path / "keys"
        manager = EncryptionManager(key_path)
        
        test_data = b"Hello, World!"
        encrypted = manager.encrypt_symmetric(test_data)
        decrypted = manager.decrypt_symmetric(encrypted)
        
        assert decrypted == test_data
        assert encrypted != test_data
    
    def test_asymmetric_encryption_decryption(self, tmp_path):
        key_path = tmp_path / "keys"
        manager = EncryptionManager(key_path)
        
        test_data = b"Hello, World!"
        encrypted = manager.encrypt_asymmetric(test_data)
        decrypted = manager.decrypt_asymmetric(encrypted)
        
        assert decrypted == test_data
        assert encrypted != test_data
    
    def test_password_hashing(self, tmp_path):
        key_path = tmp_path / "keys"
        manager = EncryptionManager(key_path)
        
        password = "test_password"
        hashed = manager.hash_password(password)
        
        assert manager.verify_password(password, hashed) is True
        assert manager.verify_password("wrong_password", hashed) is False
    
    def test_secure_token_generation(self, tmp_path):
        key_path = tmp_path / "keys"
        manager = EncryptionManager(key_path)
        
        token1 = manager.generate_secure_token()
        token2 = manager.generate_secure_token()
        
        assert len(token1) > 0
        assert token1 != token2


class TestCredentialManager:
    def test_credential_manager_creation(self, tmp_path):
        key_path = tmp_path / "keys"
        encryption_manager = EncryptionManager(key_path)
        vault_path = tmp_path / "vault"
        
        manager = CredentialManager(encryption_manager, vault_path)
        assert vault_path.exists()
    
    def test_store_and_retrieve_credential(self, tmp_path):
        key_path = tmp_path / "keys"
        encryption_manager = EncryptionManager(key_path)
        vault_path = tmp_path / "vault"
        
        manager = CredentialManager(encryption_manager, vault_path)
        
        credential_id = manager.store_credential(
            name="test_credential",
            username="test_user",
            password="test_password",
            description="Test credential",
            tags=["test", "demo"]
        )
        
        assert credential_id is not None
        
        # Retrieve the credential
        credential = manager.retrieve_credential(credential_id)
        assert credential is not None
        assert credential["name"] == "test_credential"
        assert credential["username"] == "test_user"
        assert credential["password"] == "test_password"
        assert credential["description"] == "Test credential"
        assert credential["tags"] == ["test", "demo"]
    
    def test_list_credentials(self, tmp_path):
        key_path = tmp_path / "keys"
        encryption_manager = EncryptionManager(key_path)
        vault_path = tmp_path / "vault"
        
        manager = CredentialManager(encryption_manager, vault_path)
        
        # Store multiple credentials
        manager.store_credential("cred1", "user1", "pass1")
        manager.store_credential("cred2", "user2", "pass2")
        
        credentials = manager.list_credentials()
        assert len(credentials) == 2
        assert any(c["name"] == "cred1" for c in credentials)
        assert any(c["name"] == "cred2" for c in credentials)
    
    def test_delete_credential(self, tmp_path):
        key_path = tmp_path / "keys"
        encryption_manager = EncryptionManager(key_path)
        vault_path = tmp_path / "vault"
        
        manager = CredentialManager(encryption_manager, vault_path)
        
        credential_id = manager.store_credential("test", "user", "pass")
        
        # Delete the credential
        assert manager.delete_credential(credential_id) is True
        assert manager.retrieve_credential(credential_id) is None


class TestAuditLogger:
    def test_audit_logger_creation(self, tmp_path):
        log_path = tmp_path / "audit"
        logger = AuditLogger(log_path)
        
        assert log_path.exists()
        assert logger.logger is not None
    
    def test_log_event(self, tmp_path):
        log_path = tmp_path / "audit"
        logger = AuditLogger(log_path)
        
        event = AuditEvent(
            event_type=AuditEventType.LOGIN,
            user_id="test_user",
            ip_address="127.0.0.1",
            details={"action": "login"}
        )
        
        logger.log_event(event)
        
        # Check that log file was created
        log_files = list(log_path.glob("*.log"))
        assert len(log_files) > 0
    
    def test_log_login_attempt(self, tmp_path):
        log_path = tmp_path / "audit"
        logger = AuditLogger(log_path)
        
        logger.log_login_attempt(
            username="test_user",
            ip_address="127.0.0.1",
            user_agent="test_agent",
            success=True
        )
        
        # Check that log file was created
        log_files = list(log_path.glob("*.log"))
        assert len(log_files) > 0


class TestAccessControlManager:
    def test_access_control_manager_creation(self):
        manager = AccessControlManager()
        assert manager._permissions is not None
    
    def test_has_permission(self):
        manager = AccessControlManager()
        
        # Test viewer permissions
        assert manager.has_permission(UserRole.VIEWER, "migration", "view") is True
        assert manager.has_permission(UserRole.VIEWER, "migration", "start") is False
        
        # Test admin permissions
        assert manager.has_permission(UserRole.ADMIN, "migration", "start") is True
        assert manager.has_permission(UserRole.ADMIN, "security", "manage_users") is True
        
        # Test super admin permissions
        assert manager.has_permission(UserRole.SUPER_ADMIN, "migration", "any_action") is True
    
    def test_get_user_permissions(self):
        manager = AccessControlManager()
        
        permissions = manager.get_user_permissions(UserRole.ADMIN)
        assert "migration" in permissions
        assert "config" in permissions
        assert "logs" in permissions
        assert "security" in permissions
    
    def test_add_and_remove_permission(self):
        manager = AccessControlManager()
        
        # Add permission
        manager.add_permission(UserRole.VIEWER, "test_resource", "test_action")
        assert manager.has_permission(UserRole.VIEWER, "test_resource", "test_action") is True
        
        # Remove permission
        manager.remove_permission(UserRole.VIEWER, "test_resource", "test_action")
        assert manager.has_permission(UserRole.VIEWER, "test_resource", "test_action") is False


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_rate_limiter_creation(self):
        limiter = RateLimiter(max_requests=10, window_seconds=60)
        assert limiter.max_requests == 10
        assert limiter.window_seconds == 60
    
    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        
        # Make requests within limit
        for i in range(3):
            allowed, blocked_until = await limiter.is_allowed("client1")
            assert allowed is True
            assert blocked_until is None
        
        # Exceed limit
        allowed, blocked_until = await limiter.is_allowed("client1")
        assert allowed is False
        assert blocked_until is not None
    
    @pytest.mark.asyncio
    async def test_rate_limiter_stats(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        
        # Make some requests
        await limiter.is_allowed("client1")
        await limiter.is_allowed("client1")
        
        stats = await limiter.get_client_stats("client1")
        assert stats["requests"] == 2
        assert stats["max_requests"] == 5
        assert stats["blocked"] is False


class TestSecurityScanner:
    def test_security_scanner_creation(self):
        config = SecurityConfig()
        scanner = SecurityScanner(config)
        assert scanner.config == config
    
    def test_scan_input_sql_injection(self):
        config = SecurityConfig()
        scanner = SecurityScanner(config)
        
        # Test SQL injection detection
        threats = scanner.scan_input("'; DROP TABLE users; --")
        assert len(threats) > 0
        assert any(t["type"] == "sql_injection" for t in threats)
    
    def test_scan_input_xss(self):
        config = SecurityConfig()
        scanner = SecurityScanner(config)
        
        # Test XSS detection
        threats = scanner.scan_input("<script>alert('xss')</script>")
        assert len(threats) > 0
        assert any(t["type"] == "xss" for t in threats)
    
    def test_scan_input_path_traversal(self):
        config = SecurityConfig()
        scanner = SecurityScanner(config)
        
        # Test path traversal detection
        threats = scanner.scan_input("../../../etc/passwd")
        assert len(threats) > 0
        assert any(t["type"] == "path_traversal" for t in threats)
    
    def test_validate_password_strength(self):
        config = SecurityConfig()
        scanner = SecurityScanner(config)
        
        # Test weak password
        result = scanner.validate_password_strength("weak")
        assert result["valid"] is False
        assert result["strength"] == "weak"
        
        # Test strong password
        result = scanner.validate_password_strength("StrongPass123!")
        assert result["valid"] is True
        assert result["strength"] == "strong"


class TestSecurityManager:
    def test_security_manager_creation(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        assert manager.config == config
        assert manager.encryption_manager is not None
        assert manager.credential_manager is not None
        assert manager.audit_logger is not None
        assert manager.access_control is not None
        assert manager.rate_limiter is not None
        assert manager.security_scanner is not None
    
    @pytest.mark.asyncio
    async def test_authenticate_user(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Test successful authentication
        session = await manager.authenticate_user(
            username="admin",
            password="admin123",
            ip_address="127.0.0.1",
            user_agent="test_agent"
        )
        
        assert session is not None
        assert session.username == "admin"
        assert session.role == UserRole.ADMIN
        assert session.is_active is True
        
        # Test failed authentication
        session = await manager.authenticate_user(
            username="admin",
            password="wrong_password",
            ip_address="127.0.0.1",
            user_agent="test_agent"
        )
        
        assert session is None
    
    @pytest.mark.asyncio
    async def test_get_session(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Create a session
        session = await manager.authenticate_user(
            username="admin",
            password="admin123",
            ip_address="127.0.0.1",
            user_agent="test_agent"
        )
        
        # Get the session
        retrieved_session = manager.get_session(session.session_id)
        assert retrieved_session is not None
        assert retrieved_session.session_id == session.session_id
    
    def test_logout_user(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Create a session manually
        session = UserSession(
            user_id="test_user",
            username="test_user",
            role=UserRole.OPERATOR
        )
        manager._active_sessions[session.session_id] = session
        
        # Logout the user
        manager.logout_user(session.session_id)
        
        # Verify session is removed
        assert session.session_id not in manager._active_sessions
    
    @pytest.mark.asyncio
    async def test_check_permission(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Create a session
        session = await manager.authenticate_user(
            username="admin",
            password="admin123",
            ip_address="127.0.0.1",
            user_agent="test_agent"
        )
        
        # Test permission check
        has_permission = await manager.check_permission(
            session.session_id, "migration", "start"
        )
        assert has_permission is True
        
        # Test permission check for non-existent session
        has_permission = await manager.check_permission(
            "invalid_session", "migration", "start"
        )
        assert has_permission is False
    
    @pytest.mark.asyncio
    async def test_check_rate_limit(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Test rate limiting
        allowed, blocked_until = await manager.check_rate_limit("client1")
        assert allowed is True
        assert blocked_until is None
    
    def test_scan_input(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Test input scanning
        threats = manager.scan_input("<script>alert('xss')</script>")
        assert len(threats) > 0
        assert any(t["type"] == "xss" for t in threats)
    
    def test_validate_password(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Test password validation
        result = manager.validate_password("StrongPass123!")
        assert result["valid"] is True
        assert result["strength"] == "strong"
    
    def test_encrypt_decrypt_sensitive_data(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        test_data = b"Sensitive information"
        encrypted = manager.encrypt_sensitive_data(test_data)
        decrypted = manager.decrypt_sensitive_data(encrypted)
        
        assert decrypted == test_data
        assert encrypted != test_data


class TestConvenienceFunctions:
    def test_create_security_manager(self, tmp_path):
        # Test with default configuration
        manager = create_security_manager()
        assert isinstance(manager, SecurityManager)
    
    @pytest.mark.asyncio
    async def test_secure_api_call(self, tmp_path):
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # Create a session
        session = await manager.authenticate_user(
            username="admin",
            password="admin123",
            ip_address="127.0.0.1",
            user_agent="test_agent"
        )
        
        # Test successful API call
        success, message = await secure_api_call(
            manager, session.session_id, "client1", "migration", "view"
        )
        assert success is True
        assert message == "OK"
        
        # Test API call with security threat
        success, message = await secure_api_call(
            manager, session.session_id, "client1", "migration", "view",
            input_data="<script>alert('xss')</script>"
        )
        assert success is False
        assert "Security threat detected" in message
    
    def test_encrypt_decrypt_config_value(self, tmp_path):
        key_path = tmp_path / "keys"
        encryption_manager = EncryptionManager(key_path)
        
        test_value = "sensitive_config_value"
        encrypted = encrypt_config_value(test_value, encryption_manager)
        decrypted = decrypt_config_value(encrypted, encryption_manager)
        
        assert decrypted == test_value
        assert encrypted != test_value


# Integration tests
class TestSecurityIntegration:
    @pytest.mark.asyncio
    async def test_full_security_workflow(self, tmp_path):
        """Test a complete security workflow."""
        config = SecurityConfig()
        manager = SecurityManager(config)
        
        # 1. Authenticate user
        session = await manager.authenticate_user(
            username="admin",
            password="admin123",
            ip_address="127.0.0.1",
            user_agent="test_agent"
        )
        assert session is not None
        
        # 2. Store credentials
        credential_id = manager.credential_manager.store_credential(
            name="jira_credentials",
            username="jira_user",
            password="jira_password"
        )
        assert credential_id is not None
        
        # 3. Check permissions
        has_permission = await manager.check_permission(
            session.session_id, "migration", "start"
        )
        assert has_permission is True
        
        # 4. Check rate limiting
        allowed, blocked_until = await manager.check_rate_limit("client1")
        assert allowed is True
        
        # 5. Scan input for threats
        threats = manager.scan_input("normal_input")
        assert len(threats) == 0
        
        # 6. Validate password
        result = manager.validate_password("StrongPass123!")
        assert result["valid"] is True
        
        # 7. Encrypt sensitive data
        sensitive_data = b"migration_config"
        encrypted = manager.encrypt_sensitive_data(sensitive_data)
        decrypted = manager.decrypt_sensitive_data(encrypted)
        assert decrypted == sensitive_data
        
        # 8. Logout user
        manager.logout_user(session.session_id)
        retrieved_session = manager.get_session(session.session_id)
        assert retrieved_session is None 