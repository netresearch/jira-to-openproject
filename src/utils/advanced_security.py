#!/usr/bin/env python3
"""Advanced Security Features for Jira to OpenProject Migration."""
import asyncio
import json
import logging
import re
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import bcrypt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class SecurityLevel(Enum):
    """Security levels for different operations."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class UserRole(Enum):
    """User roles for access control."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    SUPER_ADMIN = "super_admin"


class AuditEventType(Enum):
    """Types of audit events."""

    LOGIN = "login"
    LOGOUT = "logout"
    MIGRATION_START = "migration_start"
    MIGRATION_COMPLETE = "migration_complete"
    MIGRATION_FAILED = "migration_failed"
    CONFIG_CHANGE = "config_change"
    SECURITY_EVENT = "security_event"
    DATA_ACCESS = "data_access"
    SYSTEM_ACCESS = "system_access"


@dataclass
class SecurityConfig:
    """Configuration for security features."""

    encryption_key_path: Path = Path("config/security/keys")
    # Prefer centralized var/logs path; fallback is var/logs/audit
    audit_log_path: Path = Path("var/logs/audit")
    max_login_attempts: int = 5
    lockout_duration: int = 300  # seconds
    session_timeout: int = 3600  # seconds
    password_min_length: int = 12
    password_require_special: bool = True
    password_require_numbers: bool = True
    password_require_uppercase: bool = True
    rate_limit_requests: int = 100
    rate_limit_window: int = 60  # seconds
    enable_2fa: bool = True
    enable_audit_logging: bool = True
    enable_encryption: bool = True
    enable_rate_limiting: bool = True


@dataclass
class AuditEvent:
    """Audit event record."""

    event_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_type: AuditEventType = AuditEventType.SYSTEM_ACCESS
    user_id: str | None = None
    session_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    security_level: SecurityLevel = SecurityLevel.MEDIUM
    success: bool = True
    error_message: str | None = None


@dataclass
class UserSession:
    """User session information."""

    session_id: str = field(default_factory=lambda: str(uuid4()))
    user_id: str = ""
    username: str = ""
    role: UserRole = UserRole.VIEWER
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_activity: datetime = field(default_factory=lambda: datetime.now(UTC))
    ip_address: str | None = None
    user_agent: str | None = None
    is_active: bool = True


@dataclass
class RateLimitInfo:
    """Rate limiting information."""

    client_id: str
    requests: deque = field(default_factory=lambda: deque())
    blocked_until: datetime | None = None


class EncryptionManager:
    """Manages encryption and decryption of sensitive data."""

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self.key_path.mkdir(parents=True, exist_ok=True)
        self._fernet_key = None
        self._rsa_private_key = None
        self._rsa_public_key = None
        self._load_or_generate_keys()

    def _load_or_generate_keys(self) -> None:
        """Load existing keys or generate new ones."""
        fernet_key_file = self.key_path / "fernet.key"
        rsa_private_file = self.key_path / "rsa_private.pem"
        rsa_public_file = self.key_path / "rsa_public.pem"

        # Load or generate Fernet key
        if fernet_key_file.exists():
            with open(fernet_key_file, "rb") as f:
                self._fernet_key = f.read()
        else:
            self._fernet_key = Fernet.generate_key()
            with open(fernet_key_file, "wb") as f:
                f.write(self._fernet_key)

        # Load or generate RSA keys
        if rsa_private_file.exists() and rsa_public_file.exists():
            with open(rsa_private_file, "rb") as f:
                self._rsa_private_key = serialization.load_pem_private_key(
                    f.read(),
                    password=None,
                )
            with open(rsa_public_file, "rb") as f:
                self._rsa_public_key = serialization.load_pem_public_key(f.read())
        else:
            self._rsa_private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            self._rsa_public_key = self._rsa_private_key.public_key()

            # Save private key
            with open(rsa_private_file, "wb") as f:
                f.write(
                    self._rsa_private_key.private_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PrivateFormat.PKCS8,
                        encryption_algorithm=serialization.NoEncryption(),
                    ),
                )

            # Save public key
            with open(rsa_public_file, "wb") as f:
                f.write(
                    self._rsa_public_key.public_bytes(
                        encoding=serialization.Encoding.PEM,
                        format=serialization.PublicFormat.SubjectPublicKeyInfo,
                    ),
                )

    def encrypt_symmetric(self, data: bytes) -> bytes:
        """Encrypt data using symmetric encryption (Fernet)."""
        fernet = Fernet(self._fernet_key)
        return fernet.encrypt(data)

    def decrypt_symmetric(self, encrypted_data: bytes) -> bytes:
        """Decrypt data using symmetric encryption (Fernet)."""
        fernet = Fernet(self._fernet_key)
        return fernet.decrypt(encrypted_data)

    def encrypt_asymmetric(self, data: bytes) -> bytes:
        """Encrypt data using asymmetric encryption (RSA)."""
        return self._rsa_public_key.encrypt(
            data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

    def decrypt_asymmetric(self, encrypted_data: bytes) -> bytes:
        """Decrypt data using asymmetric encryption (RSA)."""
        return self._rsa_private_key.decrypt(
            encrypted_data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )

    def hash_password(self, password: str) -> str:
        """Hash a password using bcrypt."""
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode(), salt).decode()

    def verify_password(self, password: str, hashed: str) -> bool:
        """Verify a password against its hash."""
        return bcrypt.checkpw(password.encode(), hashed.encode())

    def generate_secure_token(self, length: int = 32) -> str:
        """Generate a secure random token."""
        return secrets.token_urlsafe(length)


class CredentialManager:
    """Manages secure storage and retrieval of credentials."""

    def __init__(self, encryption_manager: EncryptionManager, vault_path: Path) -> None:
        self.encryption_manager = encryption_manager
        self.vault_path = vault_path
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self._credentials_cache: dict[str, dict[str, Any]] = {}

    def store_credential(
        self,
        name: str,
        username: str,
        password: str,
        description: str = "",
        tags: list[str] | None = None,
    ) -> str:
        """Store a credential securely."""
        credential_id = str(uuid4())
        credential_data = {
            "id": credential_id,
            "name": name,
            "username": username,
            "password": password,
            "description": description,
            "tags": tags or [],
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
        }

        # Encrypt the credential data
        encrypted_data = self.encryption_manager.encrypt_symmetric(
            json.dumps(credential_data).encode(),
        )

        # Store in vault
        vault_file = self.vault_path / f"{credential_id}.enc"
        with open(vault_file, "wb") as f:
            f.write(encrypted_data)

        # Cache the credential (without password)
        safe_credential = credential_data.copy()
        safe_credential["password"] = "***ENCRYPTED***"
        self._credentials_cache[credential_id] = safe_credential

        return credential_id

    def retrieve_credential(self, credential_id: str) -> dict[str, Any] | None:
        """Retrieve a credential from the vault."""
        vault_file = self.vault_path / f"{credential_id}.enc"

        if not vault_file.exists():
            return None

        # Read and decrypt
        with open(vault_file, "rb") as f:
            encrypted_data = f.read()

        try:
            decrypted_data = self.encryption_manager.decrypt_symmetric(encrypted_data)
            return json.loads(decrypted_data.decode())
        except Exception as e:
            logging.exception(f"Failed to decrypt credential {credential_id}: {e}")
            return None

    def list_credentials(self) -> list[dict[str, Any]]:
        """List all stored credentials (without passwords)."""
        credentials = []

        for vault_file in self.vault_path.glob("*.enc"):
            credential_id = vault_file.stem
            if credential_id in self._credentials_cache:
                credentials.append(self._credentials_cache[credential_id])
            else:
                # Try to decrypt and cache
                credential_data = self.retrieve_credential(credential_id)
                if credential_data:
                    safe_credential = credential_data.copy()
                    safe_credential["password"] = "***ENCRYPTED***"
                    self._credentials_cache[credential_id] = safe_credential
                    credentials.append(safe_credential)

        return credentials

    def delete_credential(self, credential_id: str) -> bool:
        """Delete a credential from the vault."""
        vault_file = self.vault_path / f"{credential_id}.enc"

        if vault_file.exists():
            vault_file.unlink()
            self._credentials_cache.pop(credential_id, None)
            return True

        return False


class AuditLogger:
    """Manages audit logging for security events."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_path.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("audit")
        self._setup_logger()

    def _setup_logger(self) -> None:
        """Setup the audit logger."""
        self.logger.setLevel(logging.INFO)

        # File handler
        log_file = self.log_path / f"audit_{datetime.now().strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)

        # JSON formatter
        formatter = logging.Formatter(
            '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": %(message)s}',
        )
        file_handler.setFormatter(formatter)

        self.logger.addHandler(file_handler)

    def log_event(self, event: AuditEvent) -> None:
        """Log an audit event."""
        event_dict = {
            "event_id": event.event_id,
            "timestamp": event.timestamp.isoformat(),
            "event_type": event.event_type.value,
            "user_id": event.user_id,
            "session_id": event.session_id,
            "ip_address": event.ip_address,
            "user_agent": event.user_agent,
            "details": event.details,
            "security_level": event.security_level.value,
            "success": event.success,
            "error_message": event.error_message,
        }

        self.logger.info(json.dumps(event_dict))

    def log_login_attempt(
        self,
        username: str,
        ip_address: str,
        user_agent: str,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        """Log a login attempt."""
        event = AuditEvent(
            event_type=(
                AuditEventType.LOGIN if success else AuditEventType.SECURITY_EVENT
            ),
            user_id=username,
            ip_address=ip_address,
            user_agent=user_agent,
            details={"username": username, "action": "login_attempt"},
            security_level=SecurityLevel.HIGH,
            success=success,
            error_message=error_message,
        )
        self.log_event(event)

    def log_migration_event(
        self,
        event_type: AuditEventType,
        migration_id: str,
        user_id: str,
        details: dict[str, Any],
        success: bool = True,
    ) -> None:
        """Log a migration-related event."""
        event = AuditEvent(
            event_type=event_type,
            user_id=user_id,
            details={"migration_id": migration_id, **details},
            security_level=SecurityLevel.MEDIUM,
            success=success,
        )
        self.log_event(event)


class AccessControlManager:
    """Manages role-based access control."""

    def __init__(self) -> None:
        self._permissions = {
            UserRole.VIEWER: {
                "migration": ["view", "read"],
                "config": ["view", "read"],
                "logs": ["view", "read"],
                "security": ["view"],
            },
            UserRole.OPERATOR: {
                "migration": ["view", "read", "start", "stop", "pause"],
                "config": ["view", "read", "update"],
                "logs": ["view", "read", "download"],
                "security": ["view", "audit"],
            },
            UserRole.ADMIN: {
                "migration": ["view", "read", "start", "stop", "pause", "rollback"],
                "config": ["view", "read", "update", "delete", "create"],
                "logs": ["view", "read", "download", "delete"],
                "security": ["view", "audit", "manage_users", "manage_roles"],
            },
            UserRole.SUPER_ADMIN: {
                "migration": ["*"],
                "config": ["*"],
                "logs": ["*"],
                "security": ["*"],
            },
        }

    def has_permission(self, user_role: UserRole, resource: str, action: str) -> bool:
        """Check if a user role has permission for a specific action on a resource."""
        if user_role not in self._permissions:
            return False

        resource_permissions = self._permissions[user_role].get(resource, [])

        # Super admin has all permissions
        if "*" in resource_permissions:
            return True

        return action in resource_permissions

    def get_user_permissions(self, user_role: UserRole) -> dict[str, list[str]]:
        """Get all permissions for a user role."""
        return self._permissions.get(user_role, {}).copy()

    def add_permission(self, user_role: UserRole, resource: str, action: str) -> None:
        """Add a permission for a user role."""
        if user_role not in self._permissions:
            self._permissions[user_role] = {}

        if resource not in self._permissions[user_role]:
            self._permissions[user_role][resource] = []

        if action not in self._permissions[user_role][resource]:
            self._permissions[user_role][resource].append(action)

    def remove_permission(
        self,
        user_role: UserRole,
        resource: str,
        action: str,
    ) -> None:
        """Remove a permission for a user role."""
        if (
            user_role in self._permissions
            and resource in self._permissions[user_role]
            and action in self._permissions[user_role][resource]
        ):
            self._permissions[user_role][resource].remove(action)


class RateLimiter:
    """Implements API rate limiting and throttling."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._clients: dict[str, RateLimitInfo] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, client_id: str) -> tuple[bool, datetime | None]:
        """Check if a client is allowed to make a request."""
        async with self._lock:
            now = datetime.now(UTC)

            # Get or create client info
            if client_id not in self._clients:
                self._clients[client_id] = RateLimitInfo(client_id=client_id)

            client_info = self._clients[client_id]

            # Check if client is blocked
            if client_info.blocked_until and now < client_info.blocked_until:
                return False, client_info.blocked_until

            # Remove old requests outside the window
            while (
                client_info.requests
                and (now - client_info.requests[0]).total_seconds()
                > self.window_seconds
            ):
                client_info.requests.popleft()

            # Check if client has exceeded the limit
            if len(client_info.requests) >= self.max_requests:
                # Block the client
                client_info.blocked_until = now + timedelta(seconds=self.window_seconds)
                return False, client_info.blocked_until

            # Add current request
            client_info.requests.append(now)
            client_info.blocked_until = None

            return True, None

    async def get_client_stats(self, client_id: str) -> dict[str, Any]:
        """Get rate limiting statistics for a client."""
        async with self._lock:
            if client_id not in self._clients:
                return {"requests": 0, "blocked": False, "blocked_until": None}

            client_info = self._clients[client_id]
            now = datetime.now(UTC)

            # Remove old requests
            while (
                client_info.requests
                and (now - client_info.requests[0]).total_seconds()
                > self.window_seconds
            ):
                client_info.requests.popleft()

            return {
                "requests": len(client_info.requests),
                "max_requests": self.max_requests,
                "blocked": client_info.blocked_until is not None
                and now < client_info.blocked_until,
                "blocked_until": (
                    client_info.blocked_until.isoformat()
                    if client_info.blocked_until
                    else None
                ),
            }


class SecurityScanner:
    """Scans for security vulnerabilities and threats."""

    def __init__(self, config: SecurityConfig) -> None:
        self.config = config
        self._threat_patterns = {
            "sql_injection": [
                r"(\b(union|select|insert|update|delete|drop|create|alter)\b)",
                r"(\b(or|and)\b\s+\d+\s*=\s*\d+)",
                r"(\b(union|select|insert|update|delete|drop|create|alter)\b.*"
                r"\b(union|select|insert|update|delete|drop|create|alter)\b)",
            ],
            "xss": [
                r"(<script[^>]*>.*?</script>)",
                r"(javascript:)",
                r"(on\w+\s*=)",
                r"(<iframe[^>]*>)",
            ],
            "path_traversal": [
                r"(\.\./\.\./)",
                r"(\.\.\\)",
                r"(\.\.%2f)",
                r"(\.\.%5c)",
            ],
            "command_injection": [
                r"(\b(cmd|command|exec|system|eval|subprocess)\b)",
                r"(\b(rm|del|format|shutdown)\b)",
                r"(\b(ping|nslookup|traceroute|netstat)\b)",
            ],
        }

    def scan_input(self, input_data: str) -> list[dict[str, Any]]:
        """Scan input data for security threats."""
        threats = []

        for threat_type, patterns in self._threat_patterns.items():
            for pattern in patterns:
                matches = re.findall(pattern, input_data, re.IGNORECASE)
                if matches:
                    threats.append(
                        {
                            "type": threat_type,
                            "pattern": pattern,
                            "matches": matches,
                            "severity": self._get_threat_severity(threat_type),
                            "input_sample": (
                                input_data[:100] + "..."
                                if len(input_data) > 100
                                else input_data
                            ),
                        },
                    )

        return threats

    def scan_configuration(
        self,
        config_path: Path,
        scan_type: str = "configuration",
    ) -> dict[str, Any]:
        """Scan configuration files for security vulnerabilities.

        Args:
            config_path: Path to the configuration file to scan
            scan_type: Type of scan to perform ("configuration", "credentials", etc.)

        Returns:
            Dictionary containing scan results with vulnerabilities list

        """
        vulnerabilities = []

        try:
            if not config_path.exists():
                vulnerabilities.append(
                    {
                        "type": "file_not_found",
                        "severity": SecurityLevel.MEDIUM,
                        "description": f"Configuration file not found: {config_path}",
                        "recommendation": "Ensure configuration file exists and is accessible",
                    },
                )
                return {
                    "vulnerabilities": vulnerabilities,
                    "scan_type": scan_type,
                    "config_path": str(config_path),
                    "timestamp": datetime.now(UTC).isoformat(),
                }

            # Read configuration file
            with open(config_path, encoding="utf-8") as f:
                config_content = f.read()

            # Scan for common configuration security issues
            if scan_type == "configuration":
                # Check for hardcoded credentials
                credential_patterns = [
                    r"password\s*[:=]\s*['\"][^'\"]+['\"]",
                    r"api_key\s*[:=]\s*['\"][^'\"]+['\"]",
                    r"token\s*[:=]\s*['\"][^'\"]+['\"]",
                    r"secret\s*[:=]\s*['\"][^'\"]+['\"]",
                ]

                for pattern in credential_patterns:
                    matches = re.findall(pattern, config_content, re.IGNORECASE)
                    if matches:
                        vulnerabilities.append(
                            {
                                "type": "hardcoded_credentials",
                                "severity": SecurityLevel.HIGH,
                                "description": f"Found {len(matches)} potential hardcoded credentials",
                                "matches": matches[:3],  # Limit to first 3 matches
                                "recommendation": "Use environment variables or secure credential storage",
                            },
                        )

                # Check for insecure permissions
                if "permissions" in config_content.lower() and "777" in config_content:
                    vulnerabilities.append(
                        {
                            "type": "insecure_permissions",
                            "severity": SecurityLevel.MEDIUM,
                            "description": "Found potentially insecure file permissions (777)",
                            "recommendation": "Use more restrictive permissions (644, 755, etc.)",
                        },
                    )

                # Check for debug mode in production
                if (
                    "debug" in config_content.lower()
                    and "true" in config_content.lower()
                ):
                    vulnerabilities.append(
                        {
                            "type": "debug_mode_enabled",
                            "severity": SecurityLevel.MEDIUM,
                            "description": "Debug mode may be enabled",
                            "recommendation": "Disable debug mode in production environments",
                        },
                    )

            # Scan for general security threats in content
            content_threats = self.scan_input(config_content)
            for threat in content_threats:
                vulnerabilities.append(
                    {
                        "type": f"content_{threat['type']}",
                        "severity": threat["severity"],
                        "description": f"Found {threat['type']} pattern in configuration",
                        "pattern": threat["pattern"],
                        "recommendation": "Review and sanitize configuration content",
                    },
                )

        except Exception as e:
            vulnerabilities.append(
                {
                    "type": "scan_error",
                    "severity": SecurityLevel.MEDIUM,
                    "description": f"Error scanning configuration: {e!s}",
                    "recommendation": "Check file permissions and format",
                },
            )

        return {
            "vulnerabilities": vulnerabilities,
            "scan_type": scan_type,
            "config_path": str(config_path),
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _get_threat_severity(self, threat_type: str) -> SecurityLevel:
        """Get the severity level for a threat type."""
        severity_map = {
            "sql_injection": SecurityLevel.CRITICAL,
            "xss": SecurityLevel.HIGH,
            "path_traversal": SecurityLevel.HIGH,
            "command_injection": SecurityLevel.CRITICAL,
        }
        return severity_map.get(threat_type, SecurityLevel.MEDIUM)

    def validate_password_strength(self, password: str) -> dict[str, Any]:
        """Validate password strength according to security policy."""
        issues = []
        score = 0

        # Check length
        if len(password) < self.config.password_min_length:
            issues.append(
                f"Password must be at least {self.config.password_min_length} characters",
            )
        else:
            score += 1

        # Check for special characters
        if self.config.password_require_special and not any(
            c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in password
        ):
            issues.append("Password must contain at least one special character")
        else:
            score += 1

        # Check for numbers
        if self.config.password_require_numbers and not any(
            c.isdigit() for c in password
        ):
            issues.append("Password must contain at least one number")
        else:
            score += 1

        # Check for uppercase letters
        if self.config.password_require_uppercase and not any(
            c.isupper() for c in password
        ):
            issues.append("Password must contain at least one uppercase letter")
        else:
            score += 1

        # Check for lowercase letters
        if not any(c.islower() for c in password):
            issues.append("Password must contain at least one lowercase letter")
        else:
            score += 1

        # Calculate strength level
        if score >= 4 and len(issues) == 0:
            strength = "strong"
        elif score >= 3:
            strength = "medium"
        else:
            strength = "weak"

        return {
            "valid": len(issues) == 0,
            "strength": strength,
            "score": score,
            "issues": issues,
        }


class SecurityManager:
    """Main security manager that coordinates all security features."""

    def __init__(self, config: SecurityConfig) -> None:
        self.config = config
        self.encryption_manager = EncryptionManager(config.encryption_key_path)
        self.credential_manager = CredentialManager(
            self.encryption_manager,
            config.encryption_key_path / "vault",
        )
        self.audit_logger = AuditLogger(config.audit_log_path)
        self.access_control = AccessControlManager()
        self.rate_limiter = RateLimiter(
            config.rate_limit_requests,
            config.rate_limit_window,
        )
        self.security_scanner = SecurityScanner(config)
        self._active_sessions: dict[str, UserSession] = {}
        self._login_attempts: dict[str, list[datetime]] = defaultdict(list)

    async def authenticate_user(
        self,
        username: str,
        password: str,
        ip_address: str,
        user_agent: str,
    ) -> UserSession | None:
        """Authenticate a user and create a session."""
        # Check for too many login attempts
        if self._is_account_locked(username):
            self.audit_logger.log_login_attempt(
                username,
                ip_address,
                user_agent,
                False,
                "Account locked",
            )
            return None

        # TODO: Implement actual user authentication against a user store
        # For now, use a simple check against a hardcoded admin user
        if username == "admin" and password == "admin123":
            # Create session
            session = UserSession(
                user_id=username,
                username=username,
                role=UserRole.ADMIN,
                ip_address=ip_address,
                user_agent=user_agent,
            )

            self._active_sessions[session.session_id] = session

            # Log successful login
            self.audit_logger.log_login_attempt(username, ip_address, user_agent, True)

            return session
        # Log failed login attempt
        self._record_login_attempt(username)
        self.audit_logger.log_login_attempt(
            username,
            ip_address,
            user_agent,
            False,
            "Invalid credentials",
        )
        return None

    def _is_account_locked(self, username: str) -> bool:
        """Check if an account is locked due to too many failed login attempts."""
        attempts = self._login_attempts[username]
        now = datetime.now(UTC)

        # Remove old attempts outside the lockout window
        attempts[:] = [
            attempt
            for attempt in attempts
            if (now - attempt).total_seconds() < self.config.lockout_duration
        ]

        return len(attempts) >= self.config.max_login_attempts

    def _record_login_attempt(self, username: str) -> None:
        """Record a failed login attempt."""
        self._login_attempts[username].append(datetime.now(UTC))

    def get_session(self, session_id: str) -> UserSession | None:
        """Get an active session by ID."""
        session = self._active_sessions.get(session_id)

        if session and session.is_active:
            # Check if session has expired
            if (
                datetime.now(UTC) - session.last_activity
            ).total_seconds() > self.config.session_timeout:
                session.is_active = False
                return None

            # Update last activity
            session.last_activity = datetime.now(UTC)
            return session

        return None

    def logout_user(self, session_id: str) -> None:
        """Logout a user and invalidate their session."""
        if session_id in self._active_sessions:
            session = self._active_sessions[session_id]
            session.is_active = False

            self.audit_logger.log_event(
                AuditEvent(
                    event_type=AuditEventType.LOGOUT,
                    user_id=session.user_id,
                    session_id=session_id,
                    ip_address=session.ip_address,
                    user_agent=session.user_agent,
                    details={"action": "logout"},
                ),
            )

            del self._active_sessions[session_id]

    async def check_permission(
        self,
        session_id: str,
        resource: str,
        action: str,
    ) -> bool:
        """Check if a user has permission for a specific action."""
        session = self.get_session(session_id)
        if not session:
            return False

        return self.access_control.has_permission(session.role, resource, action)

    async def check_rate_limit(self, client_id: str) -> tuple[bool, datetime | None]:
        """Check if a client is within rate limits."""
        return await self.rate_limiter.is_allowed(client_id)

    def scan_input(self, input_data: str) -> list[dict[str, Any]]:
        """Scan input data for security threats."""
        return self.security_scanner.scan_input(input_data)

    def validate_password(self, password: str) -> dict[str, Any]:
        """Validate password strength."""
        return self.security_scanner.validate_password_strength(password)

    def encrypt_sensitive_data(self, data: bytes) -> bytes:
        """Encrypt sensitive data."""
        return self.encryption_manager.encrypt_symmetric(data)

    def decrypt_sensitive_data(self, encrypted_data: bytes) -> bytes:
        """Decrypt sensitive data."""
        return self.encryption_manager.decrypt_symmetric(encrypted_data)

    def log_security_event(self, event: AuditEvent) -> None:
        """Log a security event."""
        self.audit_logger.log_event(event)


# Convenience functions for easy integration
def create_security_manager(
    config_path: Path = Path("config/security.yaml"),
) -> SecurityManager:
    """Create a security manager with default configuration."""
    config = SecurityConfig()

    # Load configuration from file if it exists
    if config_path.exists():
        import yaml

        with open(config_path) as f:
            config_data = yaml.safe_load(f)
            for key, value in config_data.items():
                if hasattr(config, key):
                    setattr(config, key, value)

    return SecurityManager(config)


async def secure_api_call(
    security_manager: SecurityManager,
    session_id: str,
    client_id: str,
    resource: str,
    action: str,
    input_data: str = "",
) -> tuple[bool, str]:
    """Secure wrapper for API calls with permission and rate limit checking."""
    # Check rate limiting
    allowed, blocked_until = await security_manager.check_rate_limit(client_id)
    if not allowed:
        return False, f"Rate limit exceeded. Try again after {blocked_until}"

    # Check permissions
    has_permission = await security_manager.check_permission(
        session_id,
        resource,
        action,
    )
    if not has_permission:
        return False, "Insufficient permissions"

    # Scan input for threats
    threats = security_manager.scan_input(input_data)
    if threats:
        threat_info = ", ".join([t["type"] for t in threats])
        return False, f"Security threat detected: {threat_info}"

    return True, "OK"


def encrypt_config_value(value: str, encryption_manager: EncryptionManager) -> str:
    """Encrypt a configuration value."""
    encrypted = encryption_manager.encrypt_symmetric(value.encode())
    return encrypted.hex()


def decrypt_config_value(
    encrypted_value: str,
    encryption_manager: EncryptionManager,
) -> str:
    """Decrypt a configuration value."""
    encrypted_bytes = bytes.fromhex(encrypted_value)
    decrypted = encryption_manager.decrypt_symmetric(encrypted_bytes)
    return decrypted.decode()
