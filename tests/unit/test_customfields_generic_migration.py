import pytest

from src.application.components.customfields_generic_migration import CustomFieldsGenericMigration


class FieldsObj:
    def __init__(self) -> None:
        self.customfield_10010 = [
            {"id": "1", "name": "OptA"},
            {"id": "2", "name": "OptB"},
        ]
        self.customfield_10011 = "Free text"


class DummyIssue:
    def __init__(self, key: str) -> None:
        self.key = key
        self.fields = FieldsObj()


class DummyJira:
    def batch_get_issues(self, keys):
        return {k: DummyIssue(k) for k in keys}


class DummyOp:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_custom_field_by_name(self, name: str):
        raise Exception("not found")

    def execute_query(self, script: str):
        self.queries.append(script)
        if "cf.id" in script:
            return 801
        return True

    def ensure_wp_custom_field_id(self, name: str, field_format: str = "text") -> int:
        return 801

    def enable_custom_field_for_projects(
        self,
        cf_id: int,
        project_ids: set[int],
        cf_name: str | None = None,
    ) -> None:
        return None


@pytest.fixture(autouse=True)
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "PRJ-1": {"openproject_id": 20001},
                },
                "custom_field": {
                    "customfield_10010": {
                        "jira_id": "customfield_10010",
                        "jira_name": "Multi Option",
                        "openproject_name": "Multi Option",
                        "openproject_type": "list",
                    },
                    "customfield_10011": {
                        "jira_id": "customfield_10011",
                        "jira_name": "Text Field",
                        "openproject_name": "Text Field",
                        "openproject_type": "text",
                    },
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_customfields_generic_migration_extracts_and_sets_cf():
    mig = CustomFieldsGenericMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    assert ex.success is True
    mp = mig._map(ex)
    ld = mig._load(mp)
    assert ld.success is True
    assert ld.updated >= 1


# ---------------------------------------------------------------------------
# Issue #260: production work_package mappings are keyed by the *numeric Jira
# ID* with the human-readable key nested under ``jira_key`` — not by the human
# key directly (the legacy/test shape above).  The component iterates issues by
# human key, so a ``wp_map.get(human_key)`` lookup against an ID-keyed map
# returns ``None`` for every issue and silently skips them all.
# ---------------------------------------------------------------------------


def _set_production_wp_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the autouse mapping with the production-shaped wp_map.

    Outer key = numeric Jira ID; the human key lives under ``jira_key``.
    """
    import src.config as cfg

    class ProdMappings:
        def __init__(self) -> None:
            self._m = {
                "work_package": {
                    "144952": {"jira_key": "PRJ-1", "openproject_id": 20001},
                },
                "custom_field": {
                    "customfield_10011": {
                        "jira_id": "customfield_10011",
                        "jira_name": "Text Field",
                        "openproject_name": "Text Field",
                        "openproject_type": "text",
                    },
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

    monkeypatch.setattr(cfg, "mappings", ProdMappings(), raising=False)


def test_extract_matches_numeric_id_keyed_wp_map(monkeypatch: pytest.MonkeyPatch):
    """A numeric-ID-keyed wp_map must still resolve the issue's human key."""
    _set_production_wp_map(monkeypatch)
    mig = CustomFieldsGenericMigration(jira_client=DummyJira(), op_client=DummyOp())  # type: ignore[arg-type]
    ex = mig._extract()
    values_by_wp = (ex.data or {}).get("values_by_wp", {})
    assert values_by_wp, "production-shaped (numeric-ID) wp_map must not skip every issue"
    assert 20001 in values_by_wp


def test_load_writes_actual_cf_value_not_placeholder(monkeypatch: pytest.MonkeyPatch):
    """The real Jira CF value must reach the Ruby set-script, not 'set'."""
    _set_production_wp_map(monkeypatch)
    op = DummyOp()
    mig = CustomFieldsGenericMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    ld = mig._load(mig._map(mig._extract()))
    assert ld.updated >= 1
    set_scripts = [q for q in op.queries if "custom_field_values" in q or "custom_value_for" in q]
    assert set_scripts, "expected a CF value-setting script to be issued"
    joined = "\n".join(set_scripts)
    assert "Free text" in joined, "actual Jira CF value must be written, not discarded"
    assert "=> 'set'" not in joined, "placeholder 'set' must not replace the real value"
