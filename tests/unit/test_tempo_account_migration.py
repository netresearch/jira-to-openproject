"""Unit tests for TempoAccountMigration component.

This component does not extend BaseMigration; it has standalone helper
methods (extract_tempo_accounts, extract_openproject_companies,
create_account_mapping, create_company_in_openproject, migrate_accounts).
Tests cover happy paths plus error/empty branches.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.application.components import tempo_account_migration as tam
from src.application.components.tempo_account_migration import TempoAccountMigration


@pytest.fixture
def tempo_mig(tmp_path: Path) -> TempoAccountMigration:
    """Build a TempoAccountMigration with mocked clients and tmp data dir."""
    jira = MagicMock()
    op = MagicMock()
    mig = TempoAccountMigration(jira_client=jira, op_client=op)
    # Redirect data writes to tmp_path so json dumps don't pollute repo state.
    mig.data_dir = tmp_path
    return mig


def test_extract_tempo_accounts_returns_data_on_200(tempo_mig: TempoAccountMigration) -> None:
    """A 200 response is parsed and the accounts list is cached on self."""
    fake_accounts = [
        {"id": 1, "key": "ACCT-1", "name": "Customer A"},
        {"id": 2, "key": "ACCT-2", "name": "Customer B"},
    ]

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = fake_accounts

    with patch.object(tam.requests, "get", return_value=fake_resp):
        # The source references config.migration_config inside requests.get
        # only as a kwarg default; module-level config is a SimpleNamespace
        # without that attr by default — patch it for this call path.
        with patch.object(tam.config, "migration_config", {"ssl_verify": False}, create=True):
            accounts = tempo_mig.extract_tempo_accounts()

    assert accounts == fake_accounts
    assert tempo_mig.accounts == fake_accounts


def test_extract_tempo_accounts_returns_empty_on_non_200(
    tempo_mig: TempoAccountMigration,
) -> None:
    """A non-200 response yields an empty list rather than raising."""
    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "boom"

    with patch.object(tam.requests, "get", return_value=fake_resp):
        with patch.object(tam.config, "migration_config", {"ssl_verify": False}, create=True):
            accounts = tempo_mig.extract_tempo_accounts()

    assert accounts == []


def test_create_account_mapping_matches_by_name_case_insensitive(
    tempo_mig: TempoAccountMigration,
) -> None:
    """Existing OP companies are matched to Tempo accounts by lowercased name."""
    tempo_mig.accounts = [
        {"id": 1, "key": "ACCT-1", "name": "Acme"},
        {"id": 2, "key": "ACCT-2", "name": "Brand new"},
    ]
    tempo_mig.op_companies = [
        {"id": 100, "name": "ACME"},  # Case differs, must still match.
    ]

    mapping = tempo_mig.create_account_mapping()

    assert mapping[1]["matched_by"] == "name"
    assert mapping[1]["openproject_id"] == 100
    assert mapping[2]["matched_by"] == "none"
    assert mapping[2]["openproject_id"] is None


def test_create_company_in_openproject_dry_run_returns_placeholder(
    tempo_mig: TempoAccountMigration,
) -> None:
    """In dry_run mode no OP call is made; a placeholder dict is returned."""
    with patch.object(tam.config, "migration_config", {"dry_run": True}, create=True):
        result = tempo_mig.create_company_in_openproject(
            {"id": 1, "key": "FOO", "name": "Foo Co"},
        )

    assert result["id"] is None
    assert result["name"] == "Foo Co"
    # No real OP call happened.
    assert not tempo_mig.op_client.create_company.called


def test_create_company_in_openproject_invokes_op_client(
    tempo_mig: TempoAccountMigration,
) -> None:
    """Outside dry_run, the OP client is called with derived identifier."""
    tempo_mig.op_client.create_company.return_value = {"id": 999, "name": "Foo Co"}
    with patch.object(tam.config, "migration_config", {"dry_run": False}, create=True):
        result = tempo_mig.create_company_in_openproject(
            {"id": 1, "key": "FOO", "name": "Foo Co"},
        )

    assert result["id"] == 999
    tempo_mig.op_client.create_company.assert_called_once()
    kwargs = tempo_mig.op_client.create_company.call_args.kwargs
    assert kwargs["name"] == "Foo Co"
    # identifier derived from key, lowercased.
    assert kwargs["identifier"] == "foo"
