"""Mappings facade over :class:`MappingRepository` (ADR-002 phase 4b).

Phase 4a introduced :class:`src.domain.repositories.MappingRepository` and a
JSON-file adapter. Phase 4b makes :class:`Mappings` a thin facade over that
repository while preserving the legacy public API:

* legacy ``mappings.<name>_mapping`` attribute access (read and write) is
  served by Python ``property`` descriptors that delegate to the repository
  on first read and cache the dict for stable identity across subsequent
  reads;
* assignment to ``mappings.<name>_mapping = X`` updates the in-memory
  override without writing to disk — matching the legacy behaviour where
  only :meth:`set_mapping` persists;
* :meth:`get_mapping` / :meth:`set_mapping` / :meth:`has_mapping` accept
  legacy short names ("user", "project", "work_package", …) and resolve
  them to the repository's full stem ("user_mapping", …) via the
  :data:`SHORT_NAME_TO_STEM` table.

The op-id helpers (``get_op_user_id`` etc.) intentionally stay on this
class — they are domain-service convenience composing repository reads
with name-by-name dict lookups, and the ADR keeps :class:`MappingRepository`
deliberately minimal.

Tests can inject a :class:`FakeMappingRepository` via the new ``repo=``
keyword on :meth:`__init__`, bypassing the global ``cfg.mappings`` proxy
and the ``monkeypatch.setattr(cfg, "mappings", DummyMappings())`` ritual.
The legacy proxy stays in place; this PR only adds the seam.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from pathlib import Path
from typing import Any, ClassVar

from src.config import get_path, logger
from src.domain.repositories import MappingRepository
from src.infrastructure.persistence.mapping_repo import JsonFileMappingRepository


class Mappings:
    """Facade exposing migration mappings on top of :class:`MappingRepository`.

    The public API matches the pre-phase-4b class for backward compatibility:
    legacy attribute reads (``self.user_mapping``), dict-style access
    (``self["user_mapping"]``), the ``get_mapping`` / ``set_mapping`` /
    ``has_mapping`` / ``get_all_mappings`` helpers, and the op-id lookup
    helpers (``get_op_user_id`` etc.). Internally, all reads and writes are
    served by the injected (or default-constructed) repository.
    """

    # Define filename constants as class attributes.
    USER_MAPPING_FILE = Path("user_mapping.json")
    PROJECT_MAPPING_FILE = Path("project_mapping.json")
    ACCOUNT_MAPPING_FILE = Path("account_mapping.json")  # For parent project ID info
    COMPANY_MAPPING_FILE = Path("company_mapping.json")
    ISSUE_TYPE_MAPPING_FILE = Path("issue_type_mapping.json")
    ISSUE_TYPE_ID_MAPPING_FILE = Path("issue_type_id_mapping.json")
    STATUS_MAPPING_FILE = Path("status_mapping.json")
    LINK_TYPE_MAPPING_FILE = Path("link_type_mapping.json")
    CUSTOM_FIELD_MAPPING_FILE = Path("custom_field_mapping.json")
    SPRINT_MAPPING_FILE = Path("sprint_mapping.json")
    PRIORITY_MAPPING_FILE = Path("priority_mapping.json")
    WORK_PACKAGE_MAPPING_FILE = Path("work_package_mapping.json")  # Consolidated mapping
    WORK_PACKAGE_MAPPING_FILE_PATTERN = Path(
        "work_package_mapping_{}.json",
    )  # Per project (legacy)

    TEMPO_ACCOUNTS_FILE = Path("tempo_accounts.json")
    OP_PROJECTS_FILE = Path("openproject_projects.json")
    TEMPO_COMPANIES_FILE = Path("tempo_companies.json")

    _JSON_SUFFIX: ClassVar[str] = ".json"

    # Legacy short-name → repository stem table.
    #
    # Callers historically pass either the bare entity name ("user",
    # "project", …) or the legacy "<name>_mapping" attribute name to
    # ``get_mapping`` / ``set_mapping`` / ``has_mapping``. The repository
    # is keyed by full filename stems ("user_mapping.json" → stem
    # "user_mapping"), so we resolve here. Names not in the table fall
    # through to ``"<name>_mapping"`` so adding a new mapping does not
    # require a code change to the table.
    SHORT_NAME_TO_STEM: ClassVar[dict[str, str]] = {
        "user": "user_mapping",
        "project": "project_mapping",
        "account": "account_mapping",
        "company": "company_mapping",
        "issue_type": "issue_type_mapping",
        "issue_type_id": "issue_type_id_mapping",
        "status": "status_mapping",
        "link_type": "link_type_mapping",
        "custom_field": "custom_field_mapping",
        "sprint": "sprint_mapping",
        "priority": "priority_mapping",
        "work_package": "work_package_mapping",
    }

    # Names checked by :meth:`_warn_missing_essentials`. Kept narrow to
    # match legacy ``__init__`` warnings; expand only when ops actually
    # need a runtime alarm.
    _ESSENTIAL_STEMS: ClassVar[tuple[tuple[str, Path], ...]] = (
        ("project_mapping", PROJECT_MAPPING_FILE),
        ("issue_type_mapping", ISSUE_TYPE_MAPPING_FILE),
    )

    # Legacy mapping attribute stems exposed by :meth:`get_all_mappings`.
    # Hard-coded to preserve the legacy contract of always including
    # essentials (e.g. ``user_mapping``) even when absent from the
    # repository — older call sites rely on the dict shape rather than
    # truthy checks.
    _ALL_MAPPING_STEMS: ClassVar[tuple[str, ...]] = (
        "user_mapping",
        "project_mapping",
        "account_mapping",
        "company_mapping",
        "issue_type_mapping",
        "status_mapping",
        "link_type_mapping",
        "custom_field_mapping",
        "sprint_mapping",
        "issue_type_id_mapping",
        "work_package_mapping",
        "priority_mapping",
    )

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        repo: MappingRepository | None = None,
    ) -> None:
        """Construct a facade backed by ``repo`` (or a default JSON adapter).

        Args:
            data_dir: Directory the default repository should read/write
                under. Ignored when ``repo`` is supplied. Falls back to
                ``config.get_path("data")`` and finally to ``"data"``.
            repo: Optional repository to inject. Tests pass a
                :class:`tests.utils.fake_mapping_repository.FakeMappingRepository`
                here to avoid touching the filesystem and the global
                ``cfg.mappings`` proxy.

        """
        if data_dir is None:
            try:
                data_dir = get_path("data")
            except Exception:
                data_dir = Path("data")
        self.data_dir: Path = data_dir

        # Eagerly construct a default repository when none is injected so
        # tests that monkeypatch ``cfg.mappings`` with a plain ``Mappings``
        # subclass keep working without additional plumbing.
        self._repo: MappingRepository = repo if repo is not None else JsonFileMappingRepository(data_dir)

        # In-memory overrides for the legacy ``self.<name>_mapping``
        # attribute setter. Hydrating lazily from the repo on first read
        # gives the ``self.user_mapping["x"] = ...`` mutation pattern
        # stable identity across calls without touching disk.
        self._overrides: dict[str, dict[str, Any]] = {}

        # Surface the legacy "essentials missing" notice via repository
        # ``has`` checks, separated from ``__init__`` so the lazy-load
        # property reads stay free of side effects.
        self._warn_missing_essentials()

    # ── Private helpers ─────────────────────────────────────────────────

    def _warn_missing_essentials(self) -> None:
        """Log a notice for each essential mapping that is missing or empty.

        Matches the legacy ``__init__`` warnings (project + issue type)
        but uses :meth:`MappingRepository.has` instead of eager-loaded
        attributes, so the check itself does not pre-populate the
        in-memory override cache.
        """
        for stem, filename in self._ESSENTIAL_STEMS:
            if not self._repo.has(stem):
                logger.notice(
                    "%s (%s) is missing or empty!",
                    stem.replace("_", " ").capitalize(),
                    filename,
                )

    def _resolve_stem(self, name: str | Path) -> str:
        """Map a legacy short name (or already-full stem) to a repo stem.

        Accepts:

        * a bare entity name (``"user"``) — looked up in
          :data:`SHORT_NAME_TO_STEM`;
        * an already-full stem (``"user_mapping"``) — returned unchanged;
        * a :class:`pathlib.Path` or filename string
          (``Path("account_mapping.json")`` / ``"account_mapping.json"``)
          — the ``.json`` suffix is stripped so callers that pass the
          legacy ``Mappings.*_FILE`` constants (which are :class:`Path`
          instances) keep working.

        Anything else falls through to ``"<name>_mapping"`` so adding a
        new mapping does not require updating the resolution table.
        """
        # Normalise Path / filename inputs to a bare stem first.
        if isinstance(name, Path):
            name = name.stem
        elif isinstance(name, str) and name.endswith(self._JSON_SUFFIX):
            name = name[: -len(self._JSON_SUFFIX)]

        if name in self.SHORT_NAME_TO_STEM:
            return self.SHORT_NAME_TO_STEM[name]
        if name.endswith("_mapping"):
            return name
        return f"{name}_mapping"

    def _read(self, stem: str) -> dict[str, Any]:
        """Return the cached or freshly-loaded payload for ``stem``.

        Once a stem has been read or written through this facade, the
        same dict object is returned on every call so legacy mutation
        patterns (``self.user_mapping["k"] = v``) are visible to later
        readers without an explicit ``set_mapping`` round-trip.
        """
        if stem not in self._overrides:
            self._overrides[stem] = self._repo.get(stem)
        return self._overrides[stem]

    def _write_override(self, stem: str, value: dict[str, Any]) -> None:
        """Replace the in-memory override for ``stem`` without touching disk.

        Mirrors the legacy ``self.<name>_mapping = X`` semantics: the new
        dict is what subsequent reads see, but it is not persisted until
        :meth:`set_mapping` is called explicitly.
        """
        self._overrides[stem] = value

    @staticmethod
    def _make_mapping_property(stem: str) -> property:
        """Build a property descriptor backed by the override cache."""

        def _getter(self: Mappings) -> dict[str, Any]:
            return self._read(stem)

        def _setter(self: Mappings, value: dict[str, Any]) -> None:
            self._write_override(stem, value)

        return property(_getter, _setter)

    # ── Dict-style access (legacy __getitem__/__setitem__) ──────────────

    def __setitem__(self, key: str, value: Any) -> None:
        """Set a mapping by attribute-style key.

        Recognised keys hit the in-memory override cache (no disk write,
        matching legacy behaviour); unknown keys fall back to setting a
        plain attribute and emit a warning so the call site is visible.
        """
        if key in self._ALL_MAPPING_STEMS:
            self._write_override(key, value)
            return
        # Property setters defined at class level handle the recognised
        # legacy attribute names; anything else is treated as an opaque
        # attribute set so callers that stash auxiliary state on the
        # facade (e.g. tests) keep working.
        logger.warning("Setting unknown mapping attribute: %s", key)
        object.__setattr__(self, key, value)

    def __getitem__(self, key: str) -> Any:
        """Get a mapping by attribute-style key.

        Raises :class:`KeyError` for unknown keys to preserve legacy
        contract; callers that want a soft lookup should use
        :meth:`get_mapping`.
        """
        if key in self._ALL_MAPPING_STEMS:
            return self._read(key)
        if hasattr(self, key):
            return getattr(self, key)
        msg = f"Mapping '{key}' not found"
        raise KeyError(msg)

    # ── Compatibility shim retained for callers that still use it ───────

    def _load_mapping(self, filename: Path) -> dict[str, Any]:
        """Load a mapping by filename via the repository.

        Kept for source-level compatibility with any out-of-tree caller
        that imported the helper directly. Internally now delegates to
        :meth:`MappingRepository.get` keyed by the file stem.
        """
        return self._repo.get(filename.stem)

    # ── Op-id helpers (domain-service helpers, intentionally on facade) ──

    def get_op_project_id(self, jira_project_key: str) -> int | None:
        """Get the mapped OpenProject project ID for a Jira project key."""
        entry = self._read("project_mapping").get(jira_project_key)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira project key: %s",
            jira_project_key,
        )
        return None

    def get_op_user_id(self, jira_user_id: str) -> int | None:
        """Get the mapped OpenProject user ID for a Jira user ID."""
        # User mapping keys might be jira_user_id or jira_account_id.
        entry = self._read("user_mapping").get(jira_user_id)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira user ID: %s",
            jira_user_id,
        )
        return None

    def get_op_type_id(self, jira_issue_type_name: str) -> int | None:
        """Get the mapped OpenProject type ID for a Jira issue type name."""
        entry = self._read("issue_type_mapping").get(jira_issue_type_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira issue type name: %s",
            jira_issue_type_name,
        )
        return None

    def get_op_status_id(self, jira_status_name: str) -> int | None:
        """Get the mapped OpenProject status ID for a Jira status name."""
        entry = self._read("status_mapping").get(jira_status_name)
        if entry and entry.get("openproject_id"):
            return entry["openproject_id"]
        logger.debug(
            "No OpenProject ID found in mapping for Jira status name: %s",
            jira_status_name,
        )
        return None

    # ── Generic mapping accessors ───────────────────────────────────────

    def has_mapping(self, mapping_name: str | Path) -> bool:
        """Check whether the named mapping is present and non-empty.

        Accepts legacy short names ("project"), full stems
        ("project_mapping"), and the ``Mappings.*_FILE`` :class:`Path`
        constants. See :meth:`_resolve_stem` for the resolution rules.
        """
        stem = self._resolve_stem(mapping_name)
        # Honour in-memory overrides first so a setter that has not yet
        # been persisted is observable here, matching legacy semantics.
        if stem in self._overrides:
            return bool(self._overrides[stem])
        return self._repo.has(stem)

    def get_mapping(self, mapping_name: str | Path) -> dict[str, Any]:
        """Return the named mapping (or an empty dict if missing).

        Accepts legacy short names, full stems, and the
        ``Mappings.*_FILE`` :class:`Path` constants. Returned dict
        identity is stable across calls so callers can mutate it
        in-place — the legacy contract.
        """
        return self._read(self._resolve_stem(mapping_name))

    def get_all_mappings(self) -> dict[str, Any]:
        """Return all known mappings keyed by stem.

        Always includes the legacy "essentials" (user, project, etc.)
        even when absent from the repository, plus any extra names the
        repository surfaces via :meth:`MappingRepository.all_names`. This
        preserves the legacy shape while letting new mappings appear
        without code changes.
        """
        # Start with the legacy hardcoded list so missing essentials
        # appear as empty dicts rather than disappearing entirely —
        # several call sites read keys directly without a containment
        # check.
        result: dict[str, Any] = {stem: self._read(stem) for stem in self._ALL_MAPPING_STEMS}
        # Layer any additional repository names on top so newly-added
        # mappings (not in the legacy list) are visible.
        for stem in self._repo.all_names():
            if stem not in result:
                result[stem] = self._read(stem)
        return result

    def set_mapping(self, mapping_name: str | Path, mapping_data: MappingABC[str, Any]) -> None:
        """Persist ``mapping_data`` under ``mapping_name`` and update cache.

        Writes through to the repository (which round-trips to disk for
        the JSON adapter, or to memory for the fake) and refreshes the
        in-memory override so subsequent property reads see the new
        value without re-reading the underlying store.
        """
        stem = self._resolve_stem(mapping_name)
        try:
            data = dict(mapping_data)
            self._repo.set(stem, data)
            # Replace the override entry so legacy attribute reads match
            # what was just written. We use the same dict the repository
            # accepted to keep memory parity with the legacy "set
            # attribute then save" sequence.
            self._overrides[stem] = data
            logger.info(
                "Saved mapping '%s' with %d entries",
                mapping_name,
                len(data),
            )
        except Exception:
            logger.exception("Error saving mapping '%s'", mapping_name)
            raise


# Attach property descriptors for each legacy mapping attribute. We do
# this after class definition so the descriptor table is generated from
# the same constant the rest of the class consults, avoiding a 12-line
# block of near-identical property definitions.
for _stem in Mappings._ALL_MAPPING_STEMS:
    setattr(Mappings, _stem, Mappings._make_mapping_property(_stem))
del _stem
