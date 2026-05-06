"""``Category`` must be in the bulk-create model allowlist.

``components_migration`` migrates Jira components into OP categories via
``bulk_create_records("Category", to_create)`` — but "Category" was missing
from ``_ALLOWED_MODELS``, so ``_validate_model_name`` rejected it and the
component swallowed the exception, silently producing zero Categories.
This was visible in the live NRS re-run as a single ERROR line:
``Failed to create Categories in bulk`` followed by
``Model 'Category' is not in the allowed models list``.
"""

from __future__ import annotations

import pytest

from src.infrastructure.openproject.openproject_client import (
    _ALLOWED_MODELS,
    _validate_model_name,
)


def test_category_is_in_allowlist() -> None:
    assert "Category" in _ALLOWED_MODELS


def test_validate_model_name_accepts_category() -> None:
    """The function-level validator must not raise on ``Category``."""
    _validate_model_name("Category")


def test_validate_model_name_still_rejects_unknown_models() -> None:
    """Sanity: the allowlist still does its job."""
    with pytest.raises(ValueError, match="not in the allowed models list"):
        _validate_model_name("EvilModel")
