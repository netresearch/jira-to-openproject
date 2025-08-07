#!/usr/bin/env python3
"""Comprehensive error recovery system for Jira to OpenProject migration.

This module provides:
- Exponential backoff retry logic using tenacity
- Circuit breaker pattern using pybreaker
- Checkpointing system using SQLAlchemy
- Structured logging using structlog
- Resume functionality for interrupted migrations
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import structlog
from pybreaker import CircuitBreaker, CircuitBreakerError
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from tenacity import (
    after_log,
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Type variables
T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])

# SQLAlchemy base and models
Base = declarative_base()


class MigrationCheckpoint(Base):
    """Database model for storing migration checkpoints."""

    __tablename__ = "migration_checkpoints"

    id = Column(Integer, primary_key=True)
    migration_id = Column(String(255), nullable=False, index=True)
    checkpoint_type = Column(
        String(50),
        nullable=False,
    )  # 'issue', 'comment', 'attachment', etc.
    entity_id = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False)  # 'pending', 'completed', 'failed'
    data = Column(Text)  # JSON data for resume
    created_at = Column(DateTime, default=datetime.now(UTC))
    updated_at = Column(DateTime, default=datetime.now(UTC), onupdate=datetime.now(UTC))
    error_message = Column(Text)
    retry_count = Column(Integer, default=0)


class CheckpointManager:
    """Manages migration checkpoints for resume functionality."""

    def __init__(self, db_path: str = ".migration_checkpoints.db") -> None:
        self.db_path = Path(db_path)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def create_checkpoint(
        self,
        migration_id: str,
        checkpoint_type: str,
        entity_id: str,
        data: dict[str, Any],
    ) -> None:
        """Create a checkpoint for migration progress."""
        session = self.Session()
        try:
            checkpoint = MigrationCheckpoint(
                migration_id=migration_id,
                checkpoint_type=checkpoint_type,
                entity_id=entity_id,
                status="pending",
                data=json.dumps(data),
            )
            session.add(checkpoint)
            session.commit()
            logger.info(
                "checkpoint_created",
                migration_id=migration_id,
                checkpoint_type=checkpoint_type,
                entity_id=entity_id,
            )
        except Exception as e:
            session.rollback()
            logger.exception(
                "checkpoint_creation_failed",
                migration_id=migration_id,
                error=str(e),
            )
            raise
        finally:
            session.close()

    def update_checkpoint(
        self,
        migration_id: str,
        entity_id: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Update checkpoint status."""
        session = self.Session()
        try:
            checkpoint = (
                session.query(MigrationCheckpoint)
                .filter_by(migration_id=migration_id, entity_id=entity_id)
                .first()
            )
            if checkpoint:
                checkpoint.status = status
                checkpoint.updated_at = datetime.now(UTC)
                if error_message:
                    checkpoint.error_message = error_message
                    checkpoint.retry_count += 1
                session.commit()
                logger.info(
                    "checkpoint_updated",
                    migration_id=migration_id,
                    entity_id=entity_id,
                    status=status,
                )
        except Exception as e:
            session.rollback()
            logger.exception(
                "checkpoint_update_failed",
                migration_id=migration_id,
                error=str(e),
            )
            raise
        finally:
            session.close()

    def get_pending_checkpoints(
        self,
        migration_id: str,
        checkpoint_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get pending checkpoints for resume."""
        session = self.Session()
        try:
            query = session.query(MigrationCheckpoint).filter_by(
                migration_id=migration_id,
                status="pending",
            )
            if checkpoint_type:
                query = query.filter_by(checkpoint_type=checkpoint_type)

            checkpoints = query.all()
            return [
                {
                    "entity_id": cp.entity_id,
                    "checkpoint_type": cp.checkpoint_type,
                    "data": json.loads(cp.data) if cp.data else {},
                    "retry_count": cp.retry_count,
                }
                for cp in checkpoints
            ]
        finally:
            session.close()

    def clear_checkpoints(self, migration_id: str) -> None:
        """Clear all checkpoints for a migration."""
        session = self.Session()
        try:
            session.query(MigrationCheckpoint).filter_by(
                migration_id=migration_id,
            ).delete()
            session.commit()
            logger.info("checkpoints_cleared", migration_id=migration_id)
        except Exception as e:
            session.rollback()
            logger.exception(
                "checkpoint_clear_failed",
                migration_id=migration_id,
                error=str(e),
            )
            raise
        finally:
            session.close()


class CircuitBreakerManager:
    """Manages circuit breakers for external services."""

    def __init__(self) -> None:
        self.breakers: dict[str, CircuitBreaker] = {}

    def get_breaker(
        self,
        service_name: str,
        fail_max: int = 5,
        reset_timeout: int = 60,
        exclude: type | None = None,
    ) -> CircuitBreaker:
        """Get or create a circuit breaker for a service."""
        if service_name not in self.breakers:
            self.breakers[service_name] = CircuitBreaker(
                fail_max=fail_max,
                reset_timeout=reset_timeout,
                exclude=exclude,
                name=service_name,
            )
        return self.breakers[service_name]

    def call_with_breaker(self, service_name: str, func: F, *args, **kwargs) -> T:
        """Call a function with circuit breaker protection."""
        breaker = self.get_breaker(service_name)
        try:
            result = breaker(func)(*args, **kwargs)
            logger.info(
                "circuit_breaker_success",
                service=service_name,
                function=func.__name__,
            )
            return result
        except CircuitBreakerError as e:
            logger.exception("circuit_breaker_open", service=service_name, error=str(e))
            raise
        except Exception as e:
            logger.exception(
                "circuit_breaker_failure",
                service=service_name,
                function=func.__name__,
                error=str(e),
            )
            raise


class RetryManager:
    """Manages retry logic with exponential backoff."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def retry_with_backoff(
        self,
        retry_exceptions: tuple = (Exception,),
        before_sleep: Callable | None = None,
        after: Callable | None = None,
    ) -> Callable:
        """Decorator for retry logic with exponential backoff."""

        def decorator(func: F) -> F:
            @retry(
                stop=stop_after_attempt(self.max_attempts),
                wait=wait_exponential(multiplier=self.base_delay, max=self.max_delay),
                retry=retry_if_exception_type(retry_exceptions),
                before_sleep=before_sleep or before_sleep_log(logger, logging.WARNING),
                after=after or after_log(logger, logging.INFO),
            )
            def wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            return wrapper

        return decorator


class ErrorRecoverySystem:
    """Main error recovery system that coordinates all components."""

    def __init__(self, db_path: str = ".migration_checkpoints.db") -> None:
        self.checkpoint_manager = CheckpointManager(db_path)
        self.circuit_breaker_manager = CircuitBreakerManager()
        self.retry_manager = RetryManager()
        logger.info("error_recovery_system_initialized", db_path=db_path)

    def execute_with_recovery(
        self,
        migration_id: str,
        checkpoint_type: str,
        entity_id: str,
        func: Callable,
        *args,
        **kwargs,
    ) -> Any:
        """Execute a function with full error recovery."""
        # Create checkpoint
        checkpoint_data = {
            "args": args,
            "kwargs": kwargs,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self.checkpoint_manager.create_checkpoint(
            migration_id,
            checkpoint_type,
            entity_id,
            checkpoint_data,
        )

        try:
            # Execute with retry and circuit breaker
            service_name = f"{checkpoint_type}_service"
            result = self.circuit_breaker_manager.call_with_breaker(
                service_name,
                func,
                *args,
                **kwargs,
            )

            # Update checkpoint as successful
            self.checkpoint_manager.update_checkpoint(
                migration_id,
                entity_id,
                "completed",
            )

            logger.info(
                "execution_successful",
                migration_id=migration_id,
                entity_id=entity_id,
                checkpoint_type=checkpoint_type,
            )
            return result

        except Exception as e:
            # Update checkpoint with error
            self.checkpoint_manager.update_checkpoint(
                migration_id,
                entity_id,
                "failed",
                str(e),
            )

            logger.exception(
                "execution_failed",
                migration_id=migration_id,
                entity_id=entity_id,
                checkpoint_type=checkpoint_type,
                error=str(e),
            )
            raise

    def resume_migration(
        self,
        migration_id: str,
        checkpoint_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get pending checkpoints for migration resume."""
        return self.checkpoint_manager.get_pending_checkpoints(
            migration_id,
            checkpoint_type,
        )

    def clear_migration_data(self, migration_id: str) -> None:
        """Clear all checkpoint data for a migration."""
        self.checkpoint_manager.clear_checkpoints(migration_id)

    def get_migration_status(self, migration_id: str) -> dict[str, int]:
        """Get migration status summary."""
        session = self.checkpoint_manager.Session()
        try:
            total = (
                session.query(MigrationCheckpoint)
                .filter_by(migration_id=migration_id)
                .count()
            )
            completed = (
                session.query(MigrationCheckpoint)
                .filter_by(migration_id=migration_id, status="completed")
                .count()
            )
            failed = (
                session.query(MigrationCheckpoint)
                .filter_by(migration_id=migration_id, status="failed")
                .count()
            )
            pending = (
                session.query(MigrationCheckpoint)
                .filter_by(migration_id=migration_id, status="pending")
                .count()
            )

            return {
                "total": total,
                "completed": completed,
                "failed": failed,
                "pending": pending,
            }
        finally:
            session.close()


# Global error recovery system instance
error_recovery = ErrorRecoverySystem()
