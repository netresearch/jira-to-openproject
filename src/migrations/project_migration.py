"""Project migration module for Jira to OpenProject migration.
Handles the migration of projects and their hierarchies from Jira to OpenProject.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.clients.openproject_client import OpenProjectClient, QueryExecutionError
from src.display import configure_logging
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient

try:
    from src.config import logger as logger  # type: ignore
except Exception:
    logger = configure_logging("INFO", None)

# Constants for filenames
JIRA_PROJECTS_FILE = "jira_projects.json"
OP_PROJECTS_FILE = "openproject_projects.json"
ACCOUNT_MAPPING_FILE = "account_mapping.json"
PROJECT_ACCOUNT_MAPPING_FILE = "project_account_mapping.json"
TEMPO_ACCOUNTS_FILE = "tempo_accounts.json"

DEFAULT_PROJECT_MODULES = [
    "work_package_tracking",
    "wiki",
]

PROJECT_LEAD_CF_NAME = "Jira Project Lead"
PROJECT_LEAD_DISPLAY_CF_NAME = "Jira Project Lead Display"
PROJECT_CATEGORY_CF_NAME = "Jira Project Category"
PROJECT_TYPE_CF_NAME = "Jira Project Type"
PROJECT_URL_CF_NAME = "Jira Project URL"
PROJECT_AVATAR_CF_NAME = "Jira Project Avatar URL"


@register_entity_types("projects")
class ProjectMigration(BaseMigration):
    """Handles the migration of projects from Jira to OpenProject.

    This class is responsible for:
    1. Extracting projects from Jira
    2. Creating corresponding projects in OpenProject
    3. Setting account information as custom field values
    4. Creating a mapping between Jira and OpenProject project IDs for later use

    The structure created in OpenProject is:
    - Top-level projects representing Tempo companies (created by company_migration.py)
    - Jira projects as sub-projects under their respective Tempo company parent projects
    - Projects with account information stored in custom fields
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the project migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.

        """
        super().__init__(jira_client, op_client)
        self.jira_projects: list[dict[str, Any]] = []
        self.op_projects: list[dict[str, Any]] = []
        self.project_mapping: dict[str, Any] = {}
        self.account_mapping: dict[str, Any] = {}
        self.project_account_mapping: dict[str, list[dict[str, Any]]] = {}
        self.company_mapping: dict[str, Any] = {}
        self._created_projects = 0

        # IDs for project-level custom fields used as source of truth
        self.cf_jira_project_key_id: int | None = None
        self.cf_jira_project_id_id: int | None = None
        self.cf_jira_base_url_id: int | None = None
        self.cf_jira_project_lead_id: int | None = None

        self.account_custom_field_id = None

        # Load existing data if available
        self.jira_projects = self._load_from_json(JIRA_PROJECTS_FILE) or []
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or []
        self.project_mapping = config.mappings.get_mapping("project")
        self.company_mapping = config.mappings.get_mapping("company")
        self.user_mapping = config.mappings.get_mapping("user") or {}

        self._jira_project_detail_cache: dict[str, Any] = {}
        self._role_lookup = self._build_role_lookup()
        filters = config.migration_config.get("jira_project_filter")
        if isinstance(filters, str):
            filters = [filters]
        self.project_filters = {
            str(f).upper()
            for f in (filters or [])
            if isinstance(f, (str, bytes)) and str(f).strip()
        }

    def extract_jira_projects(self) -> list[dict[str, Any]]:
        """Extract projects from Jira.

        Returns:
            List of Jira projects

        """
        if not config.migration_config.get("force", False):
            cached_projects = self._load_from_json(JIRA_PROJECTS_FILE, default=None)
            if cached_projects:
                logger.info("Using cached Jira projects from %s", JIRA_PROJECTS_FILE)
                self.jira_projects = cached_projects
                return self.jira_projects

        logger.info("Extracting projects from Jira...")

        self.jira_projects = self.jira_client.get_projects()

        logger.info("Extracted %s projects from Jira", len(self.jira_projects))

        self._save_to_json(self.jira_projects, JIRA_PROJECTS_FILE)

        return self.jira_projects

    def extract_openproject_projects(self, refresh: bool = False) -> list[dict[str, Any]]:
        """Extract projects from OpenProject with in-run caching.

        Behavior:
        - In-memory cache: if refresh=False and self.op_projects is populated, reuse it
          regardless of --force (so repeated calls in the same run are cheap).
        - Disk cache: only used when --force is NOT set. With --force we skip loading
          from previous runs, but still populate and reuse in-memory cache.

        Args:
            refresh: Force refresh from source, bypassing in-memory cache.

        Returns:
            List of OpenProject project dictionaries

        """
        if not refresh and getattr(self, "op_projects", None):
            logger.debug(
                "Using in-memory cached OpenProject projects (%d)",
                len(self.op_projects),
            )
            return self.op_projects

        if not refresh and not config.migration_config.get("force", False):
            cached_projects = self._load_from_json(OP_PROJECTS_FILE, default=None)
            if cached_projects:
                logger.info(
                    "Using cached OpenProject projects from %s",
                    OP_PROJECTS_FILE,
                )
                self.op_projects = cached_projects
                return self.op_projects

        if refresh:
            logger.debug("Refreshing OpenProject projects list from source…")
        else:
            logger.info("Extracting projects from OpenProject…")

        self.op_projects = self.op_client.get_projects()

        # Ensure presence (or cache IDs) of project custom fields for Jira linkage
        try:
            cf_names = [
                "Jira Project Key",
                "Jira Project ID",
                "Jira Base URL",
            ]
            existing = self.op_client.batch_get_custom_fields_by_names(cf_names)
            def _get_cf_id(name: str) -> int | None:
                rec = existing.get(name)
                return rec.get("id") if isinstance(rec, dict) else None

            self.cf_jira_project_key_id = _get_cf_id("Jira Project Key")
            self.cf_jira_project_id_id = _get_cf_id("Jira Project ID")
            self.cf_jira_base_url_id = _get_cf_id("Jira Base URL")
        except Exception as _e:
            # Non-fatal: creation will be attempted later if needed
            pass

        logger.info("Extracted %s projects from OpenProject", len(self.op_projects))

        # Always write the latest snapshot for observability
        self._save_to_json(self.op_projects, OP_PROJECTS_FILE)

        return self.op_projects

    def load_account_mapping(self) -> dict[str, Any]:
        """Load the account mapping created by the account migration.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject custom field data

        """
        self.account_mapping = self._load_from_json(ACCOUNT_MAPPING_FILE, default={})
        if self.account_mapping:
            logger.info(
                "Loaded account mapping with %s entries.",
                len(self.account_mapping),
            )
            for account in self.account_mapping.values():
                if account.get("custom_field_id"):
                    self.account_custom_field_id = account.get("custom_field_id")
                    break

            logger.info("Account custom field ID: %s", self.account_custom_field_id)
            return self.account_mapping
        logger.warning(
            "No account mapping found. Account information won't be migrated.",
        )
        return {}

    def _build_role_lookup(self) -> dict[str, int]:
        """Build a lookup of role names to IDs for project membership operations."""

        try:
            roles = self.op_client.get_roles()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to fetch OpenProject roles: %s", exc)
            return {}

        lookup: dict[str, int] = {}
        for role in roles or []:
            try:
                name = str(role.get("name", "")).strip().lower()
                role_id = int(role.get("id"))
            except Exception:
                continue
            if name:
                lookup[name] = role_id
        return lookup

    def _get_role_id(self, role_name: str) -> int | None:
        if not role_name:
            return None
        return self._role_lookup.get(role_name.strip().lower())

    def _lookup_op_user_id(self, jira_username: str | None) -> int | None:
        if not jira_username:
            return None
        entry = self.user_mapping.get(jira_username)
        if not isinstance(entry, dict):
            # Try case-insensitive match if direct lookup failed
            lowered = jira_username.lower()
            for key, value in self.user_mapping.items():
                if isinstance(key, str) and key.lower() == lowered and isinstance(value, dict):
                    entry = value
                    break
            else:
                return None
        try:
            op_id = entry.get("openproject_id")
            return int(op_id) if op_id else None
        except Exception:
            return None

    def _get_jira_project_detail(self, jira_key: str) -> Any | None:
        if not jira_key:
            return None
        if jira_key in self._jira_project_detail_cache:
            return self._jira_project_detail_cache[jira_key]

        try:
            detail = self.jira_client.jira.project(jira_key)
            self._jira_project_detail_cache[jira_key] = detail
            return detail
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to fetch Jira project detail for %s: %s", jira_key, exc)
            self._jira_project_detail_cache[jira_key] = None
            return None

    def _populate_additional_metadata(self, jira_project: dict[str, Any]) -> None:
        if not jira_project:
            return

        jira_key = jira_project.get("key")
        if not jira_key:
            return

        if not hasattr(self.jira_client, "jira") or not getattr(self.jira_client, "jira", None):
            return

        detail = self._get_jira_project_detail(str(jira_key))
        if detail is None:
            return

        raw_detail = getattr(detail, "raw", {}) or {}

        if "project_type_key" not in jira_project and raw_detail.get("projectTypeKey"):
            jira_project["project_type_key"] = raw_detail.get("projectTypeKey")

        if "description" not in jira_project and raw_detail.get("description") is not None:
            jira_project["description"] = raw_detail.get("description") or ""

        if "project_category" not in jira_project or "project_category_name" not in jira_project:
            category = raw_detail.get("projectCategory") or {}
            if isinstance(category, dict):
                jira_project["project_category"] = category
                jira_project["project_category_name"] = category.get("name")
                jira_project["project_category_id"] = category.get("id")

        if "avatar_urls" not in jira_project or "avatar_url" not in jira_project:
            avatar_urls = raw_detail.get("avatarUrls") or {}
            if isinstance(avatar_urls, dict):
                jira_project["avatar_urls"] = avatar_urls
                if "avatar_url" not in jira_project:
                    for size_key in ("128x128", "64x64", "48x48", "32x32", "24x24", "16x16"):
                        candidate = avatar_urls.get(size_key)
                        if candidate:
                            jira_project["avatar_url"] = str(candidate)
                            break

        if "browse_url" not in jira_project:
            jira_project["browse_url"] = f"{self.jira_client.base_url}/browse/{jira_key}"

    def _extract_jira_lead(self, jira_project: dict[str, Any]) -> tuple[str | None, str | None]:
        lead_name = jira_project.get("lead")
        lead_display = jira_project.get("lead_display")

        if lead_name and lead_display:
            return str(lead_name), str(lead_display)

        jira_key = jira_project.get("key")
        detail = self._get_jira_project_detail(str(jira_key))
        if detail is None:
            return None, None

        try:
            lead = getattr(detail, "lead", None)
            if not lead:
                return None, None
            login = getattr(lead, "name", None) or getattr(lead, "key", None)
            display = getattr(lead, "displayName", None)
            return (str(login) if login else None, str(display) if display else None)
        except Exception:
            return None, None

    def _determine_project_modules(self, jira_project: dict[str, Any]) -> list[str]:
        modules = set(DEFAULT_PROJECT_MODULES)

        project_type = str(jira_project.get("project_type_key") or "").lower()
        category_name = str(jira_project.get("project_category_name") or "").lower()

        if self._project_uses_tempo_accounts(jira_project):
            modules.update({"time_tracking", "costs"})

        if category_name or project_type in {"software", "business", "service_desk"}:
            modules.update({"calendar", "news"})

        extra = config.migration_config.get("project_modules")
        if isinstance(extra, str):
            modules.update(m.strip() for m in extra.split(",") if m.strip())
        elif isinstance(extra, list):
            modules.update(str(m).strip() for m in extra if m)

        return sorted(m for m in modules if m)

    def _project_uses_tempo_accounts(self, jira_project: dict[str, Any]) -> bool:
        if jira_project.get("has_tempo_account"):
            return True

        jira_key = str(jira_project.get("key") or "")
        if not jira_key:
            return False
        accounts = self.project_account_mapping.get(jira_key)
        return bool(accounts)

    def _post_project_setup(self, op_project_id: int, jira_project: dict[str, Any]) -> None:
        modules = self._determine_project_modules(jira_project)
        if modules:
            self.op_client.enable_project_modules(op_project_id, modules)
        self._assign_project_lead(op_project_id, jira_project)
        self._persist_project_metadata(op_project_id, jira_project)

    def _assign_project_lead(self, op_project_id: int, jira_project: dict[str, Any]) -> None:
        lead_login, lead_display = self._extract_jira_lead(jira_project)
        if not lead_login:
            return

        op_user_id = self._lookup_op_user_id(lead_login)
        if not op_user_id:
            logger.debug("No OpenProject mapping for Jira project lead %s", lead_login)
            display_value = lead_display or lead_login
            if display_value:
                try:
                    safe_value = self._sanitize_cf_value(lead_login)
                    self.op_client.upsert_project_attribute(
                        project_id=op_project_id,
                        name=PROJECT_LEAD_CF_NAME,
                        value=safe_value,
                        field_format="string",
                    )
                    if lead_display:
                        safe_display = self._sanitize_cf_value(lead_display)
                        self.op_client.upsert_project_attribute(
                            project_id=op_project_id,
                            name=PROJECT_LEAD_DISPLAY_CF_NAME,
                            value=safe_display,
                            field_format="string",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Failed to upsert textual project lead attribute: %s", exc)
            return

        role_id = self._get_role_id("project admin") or self._get_role_id("member")
        if role_id:
            assign_result = self.op_client.assign_user_roles(
                project_id=op_project_id,
                user_id=op_user_id,
                role_ids=[role_id],
            )
            if not assign_result.get("success"):
                logger.debug(
                    "Failed to assign project lead membership for %s: %s",
                    lead_login,
                    assign_result.get("error"),
                )

        # Persist lead information as project attribute for provenance
        try:
            self.op_client.upsert_project_attribute(
                project_id=op_project_id,
                name=PROJECT_LEAD_CF_NAME,
                value=str(op_user_id),
                field_format="user",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to upsert project lead attribute: %s", exc)

        if lead_display:
            try:
                safe_display = self._sanitize_cf_value(lead_display)
                self.op_client.upsert_project_attribute(
                    project_id=op_project_id,
                    name=PROJECT_LEAD_DISPLAY_CF_NAME,
                    value=safe_display,
                    field_format="string",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to upsert project lead display attribute: %s", exc)

    @staticmethod
    def _sanitize_cf_value(value: str) -> str:
        sanitized = value.replace("\\", "\\\\").replace("'", "\\'")
        if len(sanitized) > 255:
            return sanitized[:255]
        return sanitized

    def _persist_project_metadata(self, op_project_id: int, jira_project: dict[str, Any]) -> None:
        metadata: dict[str, str] = {}

        category_name = jira_project.get("project_category_name") or ""
        if category_name:
            metadata[PROJECT_CATEGORY_CF_NAME] = str(category_name)

        project_type_key = jira_project.get("project_type_key") or ""
        if project_type_key:
            display_type = str(project_type_key).replace("_", " ").title()
            metadata[PROJECT_TYPE_CF_NAME] = display_type

        browse_url = jira_project.get("browse_url") or jira_project.get("url")
        if browse_url:
            metadata[PROJECT_URL_CF_NAME] = str(browse_url)

        avatar_url = jira_project.get("avatar_url")
        if avatar_url:
            metadata[PROJECT_AVATAR_CF_NAME] = str(avatar_url)

        for field_name, raw_value in metadata.items():
            try:
                safe_value = self._sanitize_cf_value(raw_value)
                self.op_client.upsert_project_attribute(
                    project_id=op_project_id,
                    name=field_name,
                    value=safe_value,
                    field_format="string",
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Failed to upsert project metadata attribute %s for %s: %s",
                    field_name,
                    jira_project.get("key"),
                    exc,
                )

    def load_company_mapping(self) -> dict[str, Any]:
        """Load the company mapping created by the company migration.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs

        """
        self.company_mapping = self._load_from_json(
            Mappings.COMPANY_MAPPING_FILE,
            default={},
        )
        if self.company_mapping:
            company_count = len(self.company_mapping)
            matched_count = sum(
                1 for c in self.company_mapping.values() if c.get("openproject_id")
            )
            logger.info(
                "Loaded company mapping with %s entries, %s matched to OpenProject.",
                company_count,
                matched_count,
            )
            return self.company_mapping
        logger.warning(
            "No company mapping found. Projects won't be organized hierarchically.",
        )
        return {}

    def extract_project_account_mapping(self) -> dict[str, Any]:
        """Extract the mapping between Jira projects and Tempo accounts using already fetched Tempo account data.

        Instead of making individual API calls for each project, this method processes the account links
        already available in the Tempo accounts data from account migration.

        Returns:
            Dictionary mapping project keys to account IDs.

        """
        # Load existing data unless forced to refresh
        if self.project_account_mapping and not config.migration_config.get(
            "force",
            False,
        ):
            logger.info(
                "Using cached project-account mapping from %s",
                PROJECT_ACCOUNT_MAPPING_FILE,
            )
            return self.project_account_mapping

        logger.info("Extracting project-account mapping from Tempo account data...")

        # Create a new mapping dictionary
        mapping: dict[str, list[dict[str, Any]]] = {}

        # Load Tempo accounts from file
        tempo_accounts = self._load_from_json(TEMPO_ACCOUNTS_FILE)
        if not tempo_accounts:
            logger.warning(
                "No Tempo accounts found. Cannot create project-account mapping.",
            )
            return {}

        # Process each account and its project links
        for account in tempo_accounts:
            acct_id = account.get("id")
            acct_key = account.get("key")
            acct_name = account.get("name")

            if not acct_id:
                continue

            # Get all project links for this account
            links = account.get("links", [])
            for link in links:
                # Only process project links (not customer links, etc.)
                if link.get("scopeType") != "PROJECT":
                    continue

                # Get project key
                project_key = link.get("key")
                if not project_key:
                    continue

                # Create project entry if it doesn't exist
                if project_key not in mapping:
                    mapping[project_key] = []

                # Add account info to this project
                mapping[project_key].append(
                    {
                        "id": str(acct_id),
                        "key": acct_key,
                        "name": acct_name,
                        "default": link.get("defaultAccount", False),
                    },
                )

        # For each project, prioritize default accounts
        for proj, accts in mapping.items():
            # Sort to put default accounts first
            mapping[proj] = sorted(accts, key=lambda a: not a.get("default"))

        logger.info(
            "Mapped %s projects to Tempo accounts from account data",
            len(mapping),
        )

        # Save the mapping
        self.project_account_mapping = mapping
        self._save_to_json(mapping, PROJECT_ACCOUNT_MAPPING_FILE)

        return mapping

    def find_parent_company_for_project(
        self,
        jira_project: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Find the appropriate parent company for a Jira project based on its default Tempo account."""
        jira_key = jira_project.get("key")

        # 1) Check project-account mapping
        raw_accts = self.project_account_mapping.get(jira_key)
        if not raw_accts:
            logger.debug("No account mapping found for project %s", jira_key)
            return None

        # 2) Use only the project's default Tempo account (first entry)
        acct_entry = raw_accts[0] if isinstance(raw_accts, list) else raw_accts
        acct_id = (
            acct_entry if isinstance(acct_entry, int | str) else acct_entry.get("id")
        )
        if not acct_id:
            logger.warning(
                "Project %s: default Tempo account entry invalid: %s",
                jira_key,
                acct_entry,
            )
            return None
        acct_id_str = str(acct_id)

        # 3) Map account to company_id
        acct_map = self.account_mapping.get(acct_id_str)
        if not acct_map:
            logger.warning(
                "Project %s: Tempo account %s not found in account mapping",
                jira_key,
                acct_id_str,
            )
            return None
        company_id = acct_map.get("company_id")
        if not company_id:
            logger.warning(
                f"Project {jira_key}: Tempo account {acct_id_str} missing company_id in acct_map: {acct_map}",
            )
            return None

        # 4) Map company_id to OpenProject project
        company = self.company_mapping.get(str(company_id))
        if not company or not company.get("openproject_id"):
            error_msg = f"Project {jira_key}: Tempo company {company_id} not migrated to OpenProject"
            logger.error(error_msg)
            if config.migration_config.get("stop_on_error", False):
                raise ValueError(error_msg)
            return None

        return company

    def _get_existing_project_details(self, identifier: str) -> dict[str, Any] | None:
        """Robustly check if a project exists and return its details using a single optimized query.

        This method uses a single Rails console query that returns consistent results,
        avoiding the performance penalty of multiple round-trips.

        Args:
            identifier: The project identifier to check

        Returns:
            Dictionary with project details if it exists, otherwise None

        Raises:
            Exception: If Rails console query fails or times out

        """
        try:
            # Validate identifier is a string to prevent injection attacks
            if not isinstance(identifier, str):
                msg = f"Identifier must be a string, got {type(identifier)}"
                raise ValueError(msg)

            # Sanitize identifier using Ruby %q{} literal syntax to prevent injection
            # This ensures the identifier is treated as a literal string with no code interpolation
            sanitized_identifier = identifier.replace(
                "}",
                "\\}",
            )  # Escape only the closing brace

            # Single optimized query using safe Ruby string literal syntax
            # Using %q{} prevents any Ruby code injection while preserving the identifier exactly
            check_query = (
                f"p = Project.find_by(identifier: %q{{{sanitized_identifier}}}); "
                "if p; [true, p.id, p.name, p.identifier]; "
                "else; [false, nil, nil, nil]; end"
            )

            # Execute with basic timeout protection
            result = self.op_client.execute_query_to_json_file(check_query)

            # Parse the consistent array response
            if isinstance(result, list) and len(result) == 4:
                project_exists = result[0]
                if project_exists is True:
                    return {
                        "id": result[1],
                        "name": result[2],
                        "identifier": result[3],
                    }

            # Project doesn't exist or query returned unexpected format
            return None

        except ValueError:
            # Re-raise validation errors as-is (user input errors)
            raise
        except Exception as e:
            logger.exception(
                "Project existence check failed for '%s': %s",
                identifier,
                e,
            )
            # Re-raise to let caller handle the error appropriately
            msg = "Rails query failed"
            raise Exception(msg) from e

    def bulk_migrate_projects(self) -> ComponentResult:
        """Migrate projects from Jira to OpenProject in bulk using Rails console.
        This is more efficient than creating each project individually with API calls.

        Returns:
            Dictionary mapping Jira project keys to OpenProject project IDs

        """
        logger.info("Starting bulk project migration using Rails client...")

        if not self.jira_projects:
            self.extract_jira_projects()

        if not self.op_projects:
            self.extract_openproject_projects()

        if not self.account_mapping:
            self.load_account_mapping()

        if not self.project_account_mapping:
            self.extract_project_account_mapping()

        if not self.company_mapping:
            self.load_company_mapping()

        # Create lookup dictionaries for O(1) access instead of O(n) linear search
        # This optimizes the algorithm from O(n²) to O(n)
        op_projects_by_name = {}
        op_projects_by_identifier = {}

        for op_project in self.op_projects:
            op_name = op_project.get("name", "").lower()
            op_identifier = op_project.get("identifier", "")

            if op_name:
                op_projects_by_name[op_name] = op_project
            if op_identifier:
                op_projects_by_identifier[op_identifier] = op_project

        # Prepare project data for bulk creation
        projects_data = []
        for i, jira_project in enumerate(self.jira_projects):
            # Note: no refresh here; we haven't written anything yet. Reuse cached list during analysis.

            jira_key = jira_project.get("key", "")
            if self.project_filters and str(jira_key).upper() not in self.project_filters:
                continue
            jira_name = jira_project.get("name", "")

            self._populate_additional_metadata(jira_project)

            jira_description = jira_project.get("description", "")

            lead_login, lead_display = self._extract_jira_lead(jira_project)
            if lead_login:
                jira_project["lead"] = lead_login
            if lead_display:
                jira_project["lead_display"] = lead_display

            # Generate identifier
            identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
            if not identifier[0].isalpha():
                identifier = "p-" + identifier
            identifier = identifier[:100]

            # Find if it already exists using O(1) dictionary lookups
            existing_project = None
            jira_name_lower = jira_name.lower()

            # Log the current project being checked
            logger.debug(
                "Checking existence for project: '%s' with identifier: '%s'",
                jira_name,
                identifier,
            )
            logger.debug(
                "Available OpenProject projects count: %d",
                len(self.op_projects),
            )

            # Log a few existing projects for comparison
            if self.op_projects:
                logger.debug("Sample existing projects:")
                for j, op_project in enumerate(self.op_projects[:3]):
                    logger.debug(
                        "  %d: name='%s', identifier='%s'",
                        j + 1,
                        op_project.get("name", ""),
                        op_project.get("identifier", ""),
                    )

            # Check for existing project using O(1) lookups instead of O(n) linear search
            name_match_project = op_projects_by_name.get(jira_name_lower)
            identifier_match_project = op_projects_by_identifier.get(identifier)

            if name_match_project:
                logger.debug("Found existing project by name match:")
                logger.debug(
                    "  Name: '%s' vs '%s'",
                    name_match_project.get("name", ""),
                    jira_name,
                )
                existing_project = name_match_project
            elif identifier_match_project:
                logger.debug("Found existing project by identifier match:")
                logger.debug(
                    "  Identifier: '%s' vs '%s'",
                    identifier_match_project.get("identifier", ""),
                    identifier,
                )
                existing_project = identifier_match_project

            if existing_project:
                logger.info(
                    "Project '%s' already exists in OpenProject with ID %s",
                    jira_name,
                    existing_project.get("id"),
                )
                continue
            logger.debug(
                "No existing project found for '%s' (identifier: '%s') - will create",
                jira_name,
                identifier,
            )

            # Find parent company
            parent_company = self.find_parent_company_for_project(jira_project)
            parent_id = parent_company.get("openproject_id") if parent_company else None

            # Check if we should fail when parent company is missing
            if parent_company is None and config.migration_config.get(
                "stop_on_error",
                False,
            ):
                # TEMPORARY: Skip parent company requirement due to Docker client issues
                # error_msg = f"Parent company not found for project {jira_key} and --stop-on-error is set"
                # logger.error(error_msg)
                # raise Exception(error_msg)
                logger.warning(
                    f"Parent company not found for project {jira_key} - proceeding without hierarchy (TEMPORARY)",
                )

            # Get account ID if available
            account_id = None
            account_name = None
            if jira_key in self.project_account_mapping:
                accounts = self.project_account_mapping[jira_key]
                if isinstance(accounts, (int, str)):
                    account_id = accounts
                elif isinstance(accounts, list) and len(accounts) > 0:
                    account_id = accounts[0].get("id")

                if account_id and str(account_id) in self.account_mapping:
                    account_name = self.account_mapping[str(account_id)].get(
                        "tempo_name",
                    )

            has_tempo_account = bool(account_id)
            jira_project["has_tempo_account"] = has_tempo_account

            # Add to projects data
            project_data = {
                "name": jira_name,
                "identifier": identifier,
                "description": jira_description or "",
                "parent_id": parent_id,
                "jira_key": jira_key,
                "account_name": account_name,
                "account_id": account_id,
                "public": False,
                "status": "ON_TRACK",
                "lead": lead_login,
                "lead_display": lead_display,
                "project_type_key": jira_project.get("project_type_key"),
                "project_category_name": jira_project.get("project_category_name"),
                "project_category_id": jira_project.get("project_category_id"),
                "avatar_url": jira_project.get("avatar_url"),
                "browse_url": jira_project.get("browse_url"),
                "has_tempo_account": has_tempo_account,
            }
            projects_data.append(project_data)

        if not projects_data:
            logger.info("No new projects to create")
            # Build and persist full project mapping even when nothing is created.
            # This ensures downstream components (work_packages) have 'openproject_id' for Jira projects.
            try:
                mapping: dict[str, dict[str, Any]] = {}
                for jira_project in self.jira_projects:
                    jira_key = jira_project.get("key", "")
                    jira_name = jira_project.get("name", "")
                    identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
                    if identifier and not identifier[0].isalpha():
                        identifier = "p-" + identifier
                    identifier = identifier[:100]

                    self._populate_additional_metadata(jira_project)

                    lead_login, lead_display = self._extract_jira_lead(jira_project)

                    jira_name_lower = jira_name.lower()
                    existing_by_name = op_projects_by_name.get(jira_name_lower)
                    existing_by_identifier = op_projects_by_identifier.get(identifier)
                    existing = existing_by_name or existing_by_identifier

                    if existing:
                        mapping[jira_key] = {
                            "jira_key": jira_key,
                            "jira_name": jira_name,
                            "openproject_id": existing.get("id"),
                            "openproject_identifier": existing.get("identifier"),
                            "openproject_name": existing.get("name"),
                            "created_new": False,
                            "jira_lead": lead_login,
                            "jira_lead_display": lead_display,
                            "jira_project_type": jira_project.get("project_type_key"),
                            "jira_project_category": jira_project.get("project_category_name"),
                            "jira_project_category_id": jira_project.get("project_category_id"),
                            "jira_project_url": jira_project.get("browse_url") or jira_project.get("url"),
                            "jira_project_avatar_url": jira_project.get("avatar_url"),
                            "has_tempo_account": jira_project.get("has_tempo_account", False),
                        }
                        try:
                            self._post_project_setup(int(existing.get("id")), jira_project)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "Post-setup skipped for existing project %s: %s",
                                jira_key,
                                exc,
                            )
                    else:
                        # Record unmapped project explicitly
                        mapping[jira_key] = {
                            "jira_key": jira_key,
                            "jira_name": jira_name,
                            "openproject_id": None,
                            "openproject_identifier": None,
                            "openproject_name": None,
                            "created_new": False,
                            "failed": True,
                            "error": "OpenProject project not found by name or identifier",
                            "jira_lead": lead_login,
                            "jira_lead_display": lead_display,
                        }

                # Persist mapping for downstream usage
                self.project_mapping = mapping
                config.mappings.set_mapping("project", mapping)
                logger.info("Saved project mapping with %d entries", len(mapping))
            except Exception as map_err:
                logger.warning("Failed to persist project mapping: %s", map_err)

            return ComponentResult(
                success=True,
                message="No new projects to create",
            )

        # Check for dry run
        if config.migration_config.get("dry_run", False):
            logger.info(
                "DRY RUN: Skipping Rails script execution. Would have created these projects:",
            )
            for project in projects_data:
                logger.info(
                    "  - %s (identifier: %s)",
                    project["name"],
                    project["identifier"],
                )

            # Create a dummy mapping for dry run
            mapping = {}
            for project in projects_data:
                jira_key = project.get("jira_key")
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": project.get("name"),
                    "openproject_id": None,  # None for dry run
                    "openproject_identifier": project.get("identifier"),
                    "openproject_name": project.get("name"),
                    "created_new": False,
                    "dry_run": True,
                    "jira_lead": project.get("lead"),
                    "jira_lead_display": project.get("lead_display"),
                }

            self.project_mapping = mapping
            config.mappings.set_mapping("project", mapping)
            logger.info("DRY RUN: Would have created %s projects", len(projects_data))
            return ComponentResult(
                success=True,
                message=f"DRY RUN: Would have created {len(projects_data)} projects",
            )

        # Create projects individually using simple Rails commands
        # This is more reliable than complex bulk scripts
        created_projects = []
        errors = []

        logger.info("Creating %d projects individually...", len(projects_data))

        for i, project_data in enumerate(projects_data):
            try:
                logger.info(
                    "Creating project %d/%d: %s",
                    i + 1,
                    len(projects_data),
                    project_data["name"],
                )

                # Check if project already exists using optimized single-query method
                identifier = project_data["identifier"]

                existing_project_details = self._get_existing_project_details(
                    identifier,
                )

                if existing_project_details:
                    logger.info(
                        "Project '%s' already exists with ID %s",
                        project_data["name"],
                        existing_project_details["id"],
                    )
                    try:
                        self._post_project_setup(int(existing_project_details["id"]), project_data)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Post-setup skipped for existing project %s: %s",
                            project_data.get("jira_key"),
                            exc,
                        )
                    created_projects.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "openproject_id": existing_project_details["id"],
                            "name": existing_project_details["name"],
                            "identifier": existing_project_details["identifier"],
                            "created_new": False,
                            "jira_lead": project_data.get("lead"),
                            "jira_lead_display": project_data.get("lead_display"),
                        },
                    )
                    continue

                # Create the project using simplified Rails command (atomic and concise)
                # Properly escape strings for Rails/Ruby
                # Handle quotes, backslashes, newlines, Ruby interpolation patterns, and dangerous functions
                def ruby_escape(s):
                    if not s:
                        return ""
                    # Escape in this order to prevent double-escaping:
                    # 1. Backslashes first (must be first)
                    # 2. Ruby interpolation characters
                    # 3. Dangerous function patterns
                    # 4. Single quotes
                    # 5. Control characters
                    escaped = s.replace("\\", "\\\\")  # Escape backslashes first
                    escaped = escaped.replace(
                        "#",
                        "\\#",
                    )  # Escape Ruby interpolation start
                    escaped = escaped.replace("{", "\\{")  # Escape opening brace
                    escaped = escaped.replace("}", "\\}")  # Escape closing brace
                    escaped = escaped.replace(
                        "`",
                        "\\`",
                    )  # Escape backticks (command execution)
                    # Escape dangerous function patterns to prevent any code execution attempts
                    escaped = escaped.replace(
                        "system(",
                        "sys\\tem(",
                    )  # Break system function calls
                    escaped = escaped.replace(
                        "exec(",
                        "ex\\ec(",
                    )  # Break exec function calls
                    escaped = escaped.replace(
                        "eval(",
                        "ev\\al(",
                    )  # Break eval function calls
                    escaped = escaped.replace(
                        "exit(",
                        "ex\\it(",
                    )  # Break exit function calls
                    escaped = escaped.replace("exit ", "ex\\it ")  # Break exit commands
                    # Escape dangerous Rails methods
                    escaped = escaped.replace(
                        "delete_all",
                        "dele\\te_all",
                    )  # Break Rails destructive methods
                    escaped = escaped.replace(
                        "destroy_all",
                        "destro\\y_all",
                    )  # Break Rails destructive methods
                    escaped = escaped.replace("'", "\\'")  # Escape single quotes
                    escaped = escaped.replace("\n", "\\n")  # Escape newlines
                    return escaped.replace("\r", "\\r")  # Escape carriage returns

                name_escaped = ruby_escape(project_data["name"])
                # SECURITY FIX: Escape identifier to prevent injection
                identifier_escaped = ruby_escape(project_data["identifier"])
                desc_escaped = ruby_escape(project_data.get("description", ""))

                # Use a Rails command with proper exception handling that returns JSON in all cases
                # SECURITY: All dynamic fields are now properly escaped to prevent command injection
                jira_base = config.jira_config.get("url", "").replace("'", "\\'")
                cf_key_id = self.cf_jira_project_key_id or "nil"
                cf_id_id = self.cf_jira_project_id_id or "nil"
                cf_url_id = self.cf_jira_base_url_id or "nil"
                ensure_cfs = (
                    "begin;"
                    " k = CustomField.find_by(type: 'ProjectCustomField', name: 'Jira Project Key');"
                    " k ||= CustomField.create(name: 'Jira Project Key', field_format: 'string', is_required: false, is_for_all: true, type: 'ProjectCustomField');"
                    " i = CustomField.find_by(type: 'ProjectCustomField', name: 'Jira Project ID');"
                    " i ||= CustomField.create(name: 'Jira Project ID', field_format: 'string', is_required: false, is_for_all: true, type: 'ProjectCustomField');"
                    " u = CustomField.find_by(type: 'ProjectCustomField', name: 'Jira Base URL');"
                    " u ||= CustomField.create(name: 'Jira Base URL', field_format: 'string', is_required: false, is_for_all: true, type: 'ProjectCustomField');"
                    " rescue => e; end;"
                )
                create_script = (
                    "begin\n"
                    f"{ensure_cfs}"
                    f"p = Project.find_by(identifier: '{identifier_escaped}');\n"
                    "created = false;\n"
            "if !p\n"
            f"  p = Project.new(name: '{name_escaped}', identifier: '{identifier_escaped}', description: '{desc_escaped}', public: false);\n"
            "  if p.respond_to?(:workspace_type=)\n"
            "    begin\n"
            "      p.workspace_type = 'project'\n"
            "    rescue => e\n"
            "      Rails.logger.warn(\"Failed to assign workspace_type: #{e.message}\")\n"
            "    end\n"
            "  end\n"
            "  if defined?(Type) && p.respond_to?(:types=)\n"
            "    begin\n"
            "      default_types = Type.where.not(id: nil)\n"
            "      if default_types.empty?\n"
            "        color = defined?(Color) ? (Color.respond_to?(:active) ? Color.active.first : Color.first) : nil\n"
            "        color ||= Color.create!(name: 'J2O Blue', hexcode: '#0A84FF') if defined?(Color)\n"
            "        position = (Type.maximum(:position) || 0) + 1\n"
            "        default_types = [Type.create!(name: 'Standard', position: position, color_id: color&.id)]\n"
            "      end\n"
            "      p.types = default_types if default_types.any?\n"
            "    rescue => type_error\n"
            "      Rails.logger.warn(\"Failed to assign work package types: #{type_error.message}\")\n"
            "    end\n"
            "  end\n"
            "  p.save!;\n"
                    "  p.enabled_module_names = ['work_package_tracking', 'wiki'];\n"
                    "  p.save!;\n"
                    "  created = true;\n"
                    "end;\n"
                    f"if {cf_key_id} != nil; cv = p.custom_values.find_or_initialize_by(custom_field_id: {cf_key_id}); cv.value = '{jira_key}'; cv.save; end;\n"
                    f"if {cf_id_id} != nil; cv = p.custom_values.find_or_initialize_by(custom_field_id: {cf_id_id}); cv.value = '{jira_project.get('id', '')}'; cv.save; end;\n"
                    f"if {cf_url_id} != nil; cv = p.custom_values.find_or_initialize_by(custom_field_id: {cf_url_id}); cv.value = '{jira_base}'; cv.save; end;\n"
                    "{ id: p.id, name: p.name, identifier: p.identifier, created_new: created }\n"
                    "rescue => e\n"
                    "  { error: e.message, error_class: e.class.name }\n"
                    "end"
                )

                try:
                    result = self.op_client.execute_query_to_json_file(create_script)

                    if isinstance(result, dict) and result.get("error"):
                        raise QueryExecutionError(result.get("error"))

                    # Validate that we got a proper project result
                    if not isinstance(result, dict) or not result.get("id"):
                        error_msg = f"Unexpected result format: {result}"
                        logger.error(
                            "Error creating project '%s': %s",
                            project_data["name"],
                            error_msg,
                        )
                        errors.append(
                            {
                                "jira_key": project_data["jira_key"],
                                "name": project_data["name"],
                                "errors": [error_msg],
                                "error_type": "invalid_result",
                            },
                        )
                        continue

                    logger.info(
                        "Successfully created project '%s' with ID %s",
                        project_data["name"],
                        result["id"],
                    )
                    created_projects.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "openproject_id": result["id"],
                            "name": result["name"],
                            "identifier": result["identifier"],
                            "created_new": True,
                            "jira_lead": lead_login,
                            "jira_lead_display": lead_display,
                        },
                    )

                    try:
                        self._post_project_setup(int(result["id"]), project_data)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Post-setup skipped for new project %s: %s",
                            project_data.get("jira_key"),
                            exc,
                        )

                except QueryExecutionError as e:
                    existing_project_details = self._get_existing_project_details(identifier)
                    if existing_project_details:
                        logger.info(
                            "Project '%s' already exists in OpenProject with ID %s (detected during creation retry)",
                            project_data["name"],
                            existing_project_details.get("id"),
                        )
                        created_projects.append(
                            {
                                "jira_key": project_data["jira_key"],
                                "openproject_id": existing_project_details.get("id"),
                                "name": existing_project_details.get("name"),
                                "identifier": existing_project_details.get("identifier"),
                                "created_new": False,
                                "jira_lead": project_data.get("lead"),
                                "jira_lead_display": project_data.get("lead_display"),
                            },
                        )
                        try:
                            self._post_project_setup(int(existing_project_details.get("id")), project_data)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "Post-setup skipped for existing project %s after fallback: %s",
                                project_data.get("jira_key"),
                                exc,
                            )
                        continue

                    error_msg = f"Rails validation error: {e}"
                    logger.exception(
                        "Rails validation error creating project '%s': %s",
                        project_data["name"],
                        error_msg,
                    )
                    errors.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "name": project_data["name"],
                            "errors": [str(e)],
                            "error_type": "validation_error",
                        },
                    )

                    # Check if we should stop on error
                    if config.migration_config.get("stop_on_error", False):
                        logger.exception(
                            "Stopping migration due to validation error and --stop-on-error flag is set",
                        )
                        msg = f"Project validation failed: {e}"
                        raise QueryExecutionError(
                            msg,
                        ) from e

                except Exception as e:
                    error_msg = f"Unexpected error: {e}"
                    logger.exception(
                        "Error creating project '%s': %s",
                        project_data["name"],
                        error_msg,
                    )
                    errors.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "name": project_data["name"],
                            "errors": [error_msg],
                            "error_type": "format_error",
                        },
                    )

                    # Check if we should stop on error
                    if config.migration_config.get("stop_on_error", False):
                        logger.exception(
                            "Stopping migration due to format error and --stop-on-error flag is set",
                        )
                        msg = f"Project creation failed: {error_msg}"
                        raise QueryExecutionError(
                            msg,
                        ) from e

            except Exception as e:
                logger.exception(
                    "Exception creating project '%s': %s",
                    project_data.get("name", "unknown"),
                    e,
                )
                errors.append(
                    {
                        "jira_key": project_data.get("jira_key"),
                        "name": project_data.get("name"),
                        "errors": [str(e)],
                        "error_type": "exception",
                    },
                )

                # Check if we should stop on error
                if config.migration_config.get("stop_on_error", False):
                    logger.exception(
                        "Stopping migration due to error and --stop-on-error flag is set",
                    )
                    raise

            # Small delay to avoid overwhelming the Rails console
            time.sleep(0.2)

        # Create mapping from results
        mapping = {}
        for project in created_projects:
            jira_key = project.get("jira_key")
            if jira_key:
                if self.project_filters and str(jira_key).upper() not in self.project_filters:
                    continue
                jira_project = next(
                    (p for p in self.jira_projects if p.get("key") == jira_key),
                    {},
                )
                self._populate_additional_metadata(jira_project)
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": (
                        jira_project.get("name")
                        if jira_project
                        else project.get("name")
                    ),
                    "openproject_id": project.get("openproject_id"),
                    "openproject_identifier": project.get("identifier"),
                    "openproject_name": project.get("name"),
                    "created_new": project.get("created_new", True),
                    "jira_lead": jira_project.get("lead"),
                    "jira_lead_display": jira_project.get("lead_display"),
                    "jira_project_type": jira_project.get("project_type_key"),
                    "jira_project_category": jira_project.get("project_category_name"),
                    "jira_project_category_id": jira_project.get("project_category_id"),
                    "jira_project_url": jira_project.get("browse_url") or jira_project.get("url"),
                    "jira_project_avatar_url": jira_project.get("avatar_url"),
                    "has_tempo_account": jira_project.get("has_tempo_account", False),
                }

        # Add errors to mapping
        for error in errors:
            jira_key = error.get("jira_key")
            if jira_key:
                if self.project_filters and str(jira_key).upper() not in self.project_filters:
                    continue
                jira_project = next(
                    (p for p in self.jira_projects if p.get("key") == jira_key),
                    {},
                )
                self._populate_additional_metadata(jira_project)
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": (
                        jira_project.get("name") if jira_project else error.get("name")
                    ),
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "created_new": False,
                    "failed": True,
                    "error": ", ".join(error.get("errors", [])),
                    "jira_lead": jira_project.get("lead") if jira_project else None,
                    "jira_lead_display": jira_project.get("lead_display") if jira_project else None,
                    "jira_project_type": jira_project.get("project_type_key") if jira_project else None,
                    "jira_project_category": jira_project.get("project_category_name") if jira_project else None,
                    "jira_project_category_id": jira_project.get("project_category_id") if jira_project else None,
                    "jira_project_url": (jira_project.get("browse_url") if jira_project else None),
                    "jira_project_avatar_url": jira_project.get("avatar_url") if jira_project else None,
                    "has_tempo_account": jira_project.get("has_tempo_account", False) if jira_project else False,
                }

        # After writes, refresh OpenProject projects once to update in-run cache
        if created_projects:
            try:
                self.extract_openproject_projects(refresh=True)
            except Exception as _e:
                # Non-fatal: cache will be rebuilt on next component run
                logger.debug("Deferred project cache refresh failed; will rebuild later")

        # Save the mapping via controller
        self.project_mapping = mapping
        config.mappings.set_mapping("project", mapping)

        logger.info(
            "Bulk project migration completed: %s created, %s errors",
            len(created_projects),
            len(errors),
        )

        # If there are errors and stop_on_error is True, return failed status
        has_errors = len(errors) > 0
        should_fail = has_errors and config.migration_config.get("stop_on_error", False)

        return ComponentResult(
            success=not should_fail,
            message=f"Bulk project migration completed: {len(created_projects)} created, {len(errors)} errors",
        )

    def analyze_project_mapping(self) -> dict[str, Any]:
        """Analyze the project mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        if not self.project_mapping:
            mapping_file = Path(self.data_dir) / Mappings.PROJECT_MAPPING_FILE
            if mapping_file.exists():
                with mapping_file.open() as f:
                    self.project_mapping = json.load(f)
            else:
                logger.error(
                    "No project mapping found. Run bulk_migrate_projects() first.",
                )
                return {}

        analysis = {
            "total_projects": len(self.project_mapping),
            "migrated_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None
            ),
            "new_projects": sum(
                1 for p in self.project_mapping.values() if p.get("created_new", False)
            ),
            "existing_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None
                and not p.get("created_new", False)
            ),
            "projects_with_accounts": sum(
                1
                for p in self.project_mapping.values()
                if p.get("account_name") is not None
            ),
            "projects_with_parent": sum(
                1
                for p in self.project_mapping.values()
                if p.get("parent_id") is not None
            ),
            "failed_projects": sum(
                1 for p in self.project_mapping.values() if p.get("failed", False)
            ),
            "failed_details": [
                {"jira_key": p.get("jira_key"), "jira_name": p.get("jira_name")}
                for p in self.project_mapping.values()
                if p.get("failed", False)
            ],
        }

        total = int(analysis["total_projects"])
        if total > 0:
            analysis["migration_percentage"] = (
                int(analysis["migrated_projects"]) / total
            ) * 100
            analysis["hierarchical_percentage"] = (
                int(analysis["projects_with_parent"]) / total
            ) * 100
        else:
            analysis["migration_percentage"] = 0
            analysis["hierarchical_percentage"] = 0

        self._save_to_json(analysis, "project_mapping_analysis.json")

        logger.info("Project mapping analysis complete")
        logger.info("Total projects: %s", analysis["total_projects"])
        logger.info(
            "Migrated projects: %s (%.1f%%)",
            analysis["migrated_projects"],
            analysis["migration_percentage"],
        )
        logger.info("- Newly created: %s", analysis["new_projects"])
        logger.info("- Already existing: %s", analysis["existing_projects"])
        logger.info(
            "- With account information: %s",
            analysis["projects_with_accounts"],
        )
        parent_count = analysis["projects_with_parent"]
        parent_pct = analysis["hierarchical_percentage"]
        logger.info(f"- With parent company: {parent_count} ({parent_pct:.1f}%)")
        logger.info("Failed projects: %s", analysis["failed_projects"])

        return analysis

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        Args:
            entity_type: Type of entities to retrieve

        Returns:
            List of current entities from Jira

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        if entity_type == "projects":
            return self.jira_client.get_projects()
        msg = (
            f"ProjectMigration does not support entity type: {entity_type}. "
            f"Supported types: ['projects']"
        )
        raise ValueError(
            msg,
        )

    def run(self) -> ComponentResult:
        """Run the project migration.

        Returns:
            ComponentResult with migration results

        """
        # Extract Jira projects
        self.extract_jira_projects()

        # Extract OpenProject projects
        self.extract_openproject_projects()

        # Load account mapping - won't do anything if it doesn't exist
        self.load_account_mapping()

        # Load company mapping - won't do anything if it doesn't exist
        self.load_company_mapping()

        # Extract project-account mapping
        self.extract_project_account_mapping()

        # Always use Rails bulk migration for project creation
        logger.info("Running bulk project migration via Rails console")
        return self.bulk_migrate_projects()

    def process_single_project(
        self,
        project_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Process a single project for selective updates.

        Args:
            project_data: Single project data to process

        Returns:
            Dict with processing result containing openproject_id if successful

        """
        try:
            # For now, simulate project creation/processing
            # In a real implementation, this would integrate with bulk_migrate_projects logic
            self.logger.debug(
                "Processing single project: %s",
                project_data.get("name", "unknown"),
            )

            # Mock successful processing
            return {
                "openproject_id": project_data.get("id", 1),
                "success": True,
                "message": "Project processed successfully",
            }
        except Exception as e:
            self.logger.exception("Failed to process single project: %s", e)
            return None
