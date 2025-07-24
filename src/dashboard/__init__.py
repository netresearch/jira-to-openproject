"""Dashboard package for Jira to OpenProject migration tool."""

from .app import app, get_migration_status, get_metrics

__all__ = ['app', 'get_migration_status', 'get_metrics'] 