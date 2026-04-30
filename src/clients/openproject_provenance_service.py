"""J2O Migration Provenance — store Jira→OP entity mappings as work packages.

For OpenProject entity types that cannot have custom fields attached
directly (groups, types, statuses) plus the OP entities we want a
durable record for (projects, custom fields, link types, Tempo companies
and accounts), j2o uses a special "J2O Migration Provenance" project
whose work packages encode the Jira→OP mapping. This lets the migration
restore state from OpenProject alone, without depending on local mapping
JSON files.

Phase 2c of ADR-002 split this subsystem out of the 7,000-line
``OpenProjectClient`` god-class into a focused service. The service
holds a back-reference to the client for script execution
(``execute_query``, ``execute_json_query``, ``ensure_reporting_project``,
``get_project_by_identifier``, ``ensure_custom_field`` — the latter via
``self._client.custom_fields.ensure_custom_field``). The lazy
infrastructure cache (``_cache``: project_id + type_ids + cf_ids) lives
here too.

``OpenProjectClient`` exposes the service via ``self.provenance`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.clients.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectProvenanceService:
    """Manages the J2O Migration Provenance project, types, CFs, and records."""

    PROJECT_IDENTIFIER: str = "j2o-migration-provenance"
    PROJECT_NAME: str = "J2O Migration Provenance"

    # Entity types tracked in the provenance registry. Note:
    # - ``company`` and ``account`` create OP Projects but originate from Tempo.
    # - ``custom_field`` and ``link_type`` track CF creation for Jira→OP field mapping.
    ENTITY_TYPES: tuple[str, ...] = (
        "project",
        "group",
        "type",
        "status",
        "company",
        "account",
        "custom_field",
        "link_type",
    )

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger
        # Lazy-init: populated on first record_entity_provenance call.
        self._cache: dict[str, Any] | None = None

    # ── infrastructure ensurance ──────────────────────────────────────────

    def ensure_migration_project(self) -> int:
        """Ensure the J2O Migration Provenance project exists.

        Returns the OpenProject project ID.
        """
        return self._client.ensure_reporting_project(
            identifier=self.PROJECT_IDENTIFIER,
            name=self.PROJECT_NAME,
        )

    @staticmethod
    def _entity_type_label(entity_type: str) -> str:
        """Capitalised label used in CF/type names.

        ``str.title()`` over-capitalises underscore-separated entity types
        (``"custom_field"`` → ``"Custom_Field"``), while
        ``ensure_provenance_custom_fields()`` already named the CFs with
        ``"Custom_field"`` / ``"Link_type"`` (lowercase second word). So all
        record/restore/bulk paths must use ``.capitalize()`` to produce
        labels that match what was created. Centralising the transformation
        here so the four call sites can't drift out of sync again.
        """
        return entity_type.capitalize()

    def ensure_provenance_types(self, project_id: int) -> dict[str, int]:
        """Ensure WorkPackage types exist for each provenance entity type.

        Creates types like ``J2O Project Mapping``, ``J2O Group Mapping``,
        ``J2O Custom_field Mapping``. Uses ``.capitalize()`` (not
        ``.title()``) so underscore-separated entity types produce labels
        that match the CF names created in ``ensure_provenance_custom_fields``.

        Args:
            project_id: The J2O Migration project ID

        Returns:
            Dict mapping entity type to OP type ID

        """
        type_ids: dict[str, int] = {}

        for entity_type in self.ENTITY_TYPES:
            type_name = f"J2O {self._entity_type_label(entity_type)} Mapping"

            script = (
                "begin\n"
                f"  type_name = '{type_name}'\n"
                f"  project_id = {project_id}\n"
                "  wp_type = Type.find_by(name: type_name)\n"
                "  unless wp_type\n"
                "    wp_type = Type.create!(name: type_name, is_default: false, is_milestone: false)\n"
                "  end\n"
                "  project = Project.find(project_id)\n"
                "  unless project.types.include?(wp_type)\n"
                "    project.types << wp_type\n"
                "  end\n"
                "  { id: wp_type.id, name: wp_type.name }\n"
                "rescue => e\n"
                "  { error: e.message }\n"
                "end\n"
            )

            try:
                result = self._client.execute_json_query(script)
                if isinstance(result, dict) and result.get("id"):
                    type_ids[entity_type] = int(result["id"])
                    self._logger.debug("Ensured J2O type '%s' with ID %d", type_name, type_ids[entity_type])
                elif isinstance(result, dict) and result.get("error"):
                    self._logger.warning("Failed to ensure J2O type '%s': %s", type_name, result["error"])
            except Exception as e:
                self._logger.warning("Error ensuring J2O type '%s': %s", type_name, e)

        return type_ids

    def ensure_provenance_custom_fields(self) -> dict[str, int]:
        """Ensure custom fields for OP entity ID mapping exist.

        Creates CFs like ``J2O OP Project ID``, ``J2O OP Group ID``, etc.,
        plus a generic ``J2O Entity Type`` filtering CF.

        Returns:
            Dict mapping CF name to its CF ID.

        """
        cf_ids: dict[str, int] = {}

        # Fields for mapping to OP entity IDs
        cf_specs = [
            ("J2O OP Project ID", "int"),
            ("J2O OP Group ID", "int"),
            ("J2O OP Type ID", "int"),
            ("J2O OP Status ID", "int"),
            ("J2O OP Company ID", "int"),  # Tempo Company → OP Project ID
            ("J2O OP Account ID", "int"),  # Tempo Account → OP Project ID
            ("J2O OP Custom_field ID", "int"),  # Jira CF ID → OP CustomField ID
            ("J2O OP Link_type ID", "int"),  # Jira Link Type ID → OP CustomField ID
            ("J2O Entity Type", "string"),  # Entity type for filtering
        ]

        for name, fmt in cf_specs:
            try:
                result = self._client.custom_fields.ensure_custom_field(
                    name,
                    field_format=fmt,
                    cf_type="WorkPackageCustomField",
                )
                if isinstance(result, dict) and result.get("id"):
                    cf_ids[name] = int(result["id"])
            except Exception as e:
                self._logger.warning("Failed ensuring provenance CF '%s': %s", name, e)

        return cf_ids

    # ── recording / restoring ─────────────────────────────────────────────

    def record_entity(
        self,
        *,
        entity_type: str,
        jira_key: str,
        jira_id: str | None = None,
        op_entity_id: int,
        jira_name: str | None = None,
    ) -> dict[str, Any]:
        """Record provenance for an entity by creating/updating a work package.

        Lazy-initialises the project / types / CFs the first time it's called.
        """
        # Lazy import avoids the openproject_client ↔ this-module import cycle.
        from src.clients.openproject_client import escape_ruby_single_quoted

        if entity_type not in self.ENTITY_TYPES:
            msg = f"Invalid entity type: {entity_type}. Must be one of {self.ENTITY_TYPES}"
            raise ValueError(msg)

        # Ensure infrastructure exists (cached after first call)
        if self._cache is None:
            project_id = self.ensure_migration_project()
            self._cache = {
                "project_id": project_id,
                "type_ids": self.ensure_provenance_types(project_id),
                "cf_ids": self.ensure_provenance_custom_fields(),
            }
        project_id = self._cache["project_id"]
        type_ids = self._cache["type_ids"]
        cf_ids = self._cache["cf_ids"]

        type_id = type_ids.get(entity_type)
        if not type_id:
            msg = f"Failed to get type ID for {entity_type}"
            raise QueryExecutionError(msg)

        # Build work package subject (unique identifier for this mapping)
        subject = f"{entity_type.upper()}: {jira_key}"
        if jira_name:
            subject = f"{subject} ({jira_name})"

        # Get CF IDs for the mapping fields. ``capitalize`` (not ``title``)
        # so ``custom_field`` -> ``Custom_field`` matches the CF name created
        # in ensure_provenance_custom_fields.
        cf_op_id_field = f"J2O OP {self._entity_type_label(entity_type)} ID"
        cf_op_id = cf_ids.get(cf_op_id_field)
        cf_entity_type_id = cf_ids.get("J2O Entity Type")

        # Build cf_values assignments as independent optional lines. The
        # previous chained-conditional form silently truncated the script
        # (Python parsed the whole tail of the script string concatenation
        # as the else-branch of an outer ``if`` expression), dropping the
        # ``wp.save!`` and ``rescue/end`` lines whenever cf_op_id was set —
        # so provenance writes were never persisting. Each line is now
        # emitted independently, with Ruby-side ``if cf_id`` guards.
        cf_value_lines = ""
        if cf_op_id:
            cf_value_lines += f"  cf_values[{cf_op_id}] = {op_entity_id} if {cf_op_id}\n"
        if cf_entity_type_id:
            cf_value_lines += (
                f"  cf_values[{cf_entity_type_id}] = "
                f"'{escape_ruby_single_quoted(entity_type)}' if {cf_entity_type_id}\n"
            )

        script = (
            "begin\n"
            f"  project = Project.find({project_id})\n"
            f"  wp_type = Type.find({type_id})\n"
            f"  subject = '{escape_ruby_single_quoted(subject)}'\n"
            "  status = Status.default || Status.first\n"
            "  priority = IssuePriority.default || IssuePriority.first\n"
            "  # Find existing or create new\n"
            f"  wp = project.work_packages.where(type_id: {type_id}).find_by(subject: subject)\n"
            "  created = false\n"
            "  if wp.nil?\n"
            "    wp = WorkPackage.new(\n"
            "      project: project,\n"
            "      type: wp_type,\n"
            "      subject: subject,\n"
            "      status: status,\n"
            "      priority: priority,\n"
            "      author: User.admin.first || User.first\n"
            "    )\n"
            "    created = true\n"
            "  end\n"
            "  # Set custom field values\n"
            "  cf_values = {}\n"
            f"{cf_value_lines}"
            "  wp.custom_field_values = cf_values if cf_values.any?\n"
            "  wp.save!\n"
            "  { success: true, id: wp.id, subject: wp.subject, created: created }\n"
            "rescue => e\n"
            "  { success: false, error: e.message, backtrace: e.backtrace.first(3) }\n"
            "end\n"
        )

        try:
            result = self._client.execute_json_query(script)
            if isinstance(result, dict):
                if result.get("success"):
                    action = "Created" if result.get("created") else "Updated"
                    self._logger.debug(
                        "%s provenance WP for %s '%s' → OP ID %d",
                        action,
                        entity_type,
                        jira_key,
                        op_entity_id,
                    )
                return result
            return {"success": False, "error": f"Unexpected result: {result}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def restore_entity_mappings(self, entity_type: str) -> dict[str, dict[str, Any]]:
        """Restore Jira→OP mappings by querying provenance work packages.

        Args:
            entity_type: One of the configured ``ENTITY_TYPES``.

        Returns:
            Dict mapping Jira key → mapping data. Empty dict if the J2O
            Migration project doesn't exist or no provenance data is found.

        """
        if entity_type not in self.ENTITY_TYPES:
            msg = f"Invalid entity type: {entity_type}. Must be one of {self.ENTITY_TYPES}"
            raise ValueError(msg)

        # Get the project (may not exist if never recorded).
        try:
            project_result = self._client.get_project_by_identifier(self.PROJECT_IDENTIFIER)
            if not project_result or not project_result.get("id"):
                self._logger.info("J2O Migration project not found - no provenance data available")
                return {}
            project_id = int(project_result["id"])
        except Exception:
            self._logger.debug("J2O Migration project not found")
            return {}

        # Use ``capitalize()`` (not ``title()``) — see _entity_type_label.
        type_name = f"J2O {self._entity_type_label(entity_type)} Mapping"
        cf_op_id_field = f"J2O OP {self._entity_type_label(entity_type)} ID"

        script = (
            "begin\n"
            f"  project = Project.find({project_id})\n"
            f"  wp_type = Type.find_by(name: '{type_name}')\n"
            "  return [] unless wp_type\n"
            f"  cf_op_id = CustomField.find_by(name: '{cf_op_id_field}', type: 'WorkPackageCustomField')\n"
            "  cf_entity_type = CustomField.find_by(name: 'J2O Entity Type', type: 'WorkPackageCustomField')\n"
            "  # Also get J2O Origin fields for full provenance\n"
            "  cf_origin_key = CustomField.find_by(name: 'J2O Origin Key', type: 'WorkPackageCustomField')\n"
            "  cf_origin_id = CustomField.find_by(name: 'J2O Origin ID', type: 'WorkPackageCustomField')\n"
            "  cf_origin_system = CustomField.find_by(name: 'J2O Origin System', type: 'WorkPackageCustomField')\n"
            f"  wps = project.work_packages.where(type_id: wp_type.id)\n"
            "  wps.map do |wp|\n"
            "    {\n"
            "      id: wp.id,\n"
            "      subject: wp.subject,\n"
            "      op_entity_id: (cf_op_id ? wp.custom_value_for(cf_op_id)&.value&.to_i : nil),\n"
            "      entity_type: (cf_entity_type ? wp.custom_value_for(cf_entity_type)&.value : nil),\n"
            "      j2o_origin_key: (cf_origin_key ? wp.custom_value_for(cf_origin_key)&.value : nil),\n"
            "      j2o_origin_id: (cf_origin_id ? wp.custom_value_for(cf_origin_id)&.value : nil),\n"
            "      j2o_origin_system: (cf_origin_system ? wp.custom_value_for(cf_origin_system)&.value : nil)\n"
            "    }\n"
            "  end\n"
            "rescue => e\n"
            "  { error: e.message }\n"
            "end\n"
        )

        try:
            result = self._client.execute_json_query(script)
            if isinstance(result, dict) and result.get("error"):
                self._logger.warning("Error restoring %s mappings: %s", entity_type, result["error"])
                return {}

            if not isinstance(result, list):
                return {}

            # Build mapping from subject parsing and CF values
            mappings: dict[str, dict[str, Any]] = {}
            for wp in result:
                # Parse subject to extract Jira key: "TYPE: jira-key (name)" or "TYPE: jira-key"
                subject = wp.get("subject", "")
                prefix = f"{entity_type.upper()}: "
                if subject.startswith(prefix):
                    rest = subject[len(prefix) :]
                    # Handle "(name)" suffix
                    if " (" in rest and rest.endswith(")"):
                        jira_key = rest.split(" (")[0]
                        jira_name = rest.split(" (")[1].rstrip(")")
                    else:
                        jira_key = rest
                        jira_name = None

                    mappings[jira_key] = {
                        "jira_key": jira_key,
                        "jira_name": jira_name,
                        "openproject_id": wp.get("op_entity_id"),
                        "matched_by": "j2o_provenance",
                        "j2o_origin_key": wp.get("j2o_origin_key"),
                        "j2o_origin_id": wp.get("j2o_origin_id"),
                        "j2o_origin_system": wp.get("j2o_origin_system"),
                        "restored_from_op": True,
                        "provenance_wp_id": wp.get("id"),
                    }

            self._logger.info("Restored %d %s mappings from provenance", len(mappings), entity_type)
            return mappings

        except Exception as e:
            self._logger.warning("Failed to restore %s mappings from provenance: %s", entity_type, e)
            return {}

    def bulk_record_entities(
        self,
        entity_type: str,
        mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk record provenance for multiple entities in one Rails call.

        Args:
            entity_type: One of the configured ``ENTITY_TYPES``.
            mappings: Each dict needs ``jira_key`` and ``op_entity_id`` (or
                ``openproject_id``); ``jira_name`` is optional.

        Returns:
            Dict with ``success`` count, ``failed`` count, and ``errors`` (max 10).

        """
        # Lazy import avoids the openproject_client ↔ this-module import cycle.
        from src.clients.openproject_client import escape_ruby_single_quoted

        if entity_type not in self.ENTITY_TYPES:
            msg = f"Invalid entity type: {entity_type}. Must be one of {self.ENTITY_TYPES}"
            raise ValueError(msg)

        if not mappings:
            return {"success": 0, "failed": 0, "errors": []}

        # Ensure infrastructure exists (once for all)
        project_id = self.ensure_migration_project()
        type_ids = self.ensure_provenance_types(project_id)
        cf_ids = self.ensure_provenance_custom_fields()

        type_id = type_ids.get(entity_type)
        if not type_id:
            return {"success": 0, "failed": len(mappings), "errors": [f"No type ID for {entity_type}"]}

        cf_op_id_field = f"J2O OP {self._entity_type_label(entity_type)} ID"
        cf_op_id = cf_ids.get(cf_op_id_field)
        cf_entity_type_id = cf_ids.get("J2O Entity Type")

        # Build Ruby array of mappings
        ruby_mappings = []
        for m in mappings:
            jira_key = m.get("jira_key", "")
            jira_name = m.get("jira_name", "")
            op_entity_id = m.get("op_entity_id") or m.get("openproject_id")
            if jira_key and op_entity_id:
                subject = f"{entity_type.upper()}: {jira_key}"
                if jira_name:
                    subject = f"{subject} ({jira_name})"
                ruby_mappings.append(
                    f"  {{ subject: '{escape_ruby_single_quoted(subject)}', op_entity_id: {op_entity_id} }}",
                )

        if not ruby_mappings:
            return {"success": 0, "failed": 0, "errors": []}

        script = (
            "begin\n"
            f"  project = Project.find({project_id})\n"
            f"  wp_type = Type.find({type_id})\n"
            "  status = Status.default || Status.first\n"
            "  priority = IssuePriority.default || IssuePriority.first\n"
            "  author = User.admin.first || User.first\n"
            f"  cf_op_id = {cf_op_id or 'nil'}\n"
            f"  cf_entity_type_id = {cf_entity_type_id or 'nil'}\n"
            "  mappings = [\n" + ",\n".join(ruby_mappings) + "\n  ]\n"
            "  success = 0\n"
            "  failed = 0\n"
            "  errors = []\n"
            "  mappings.each do |m|\n"
            "    begin\n"
            "      wp = project.work_packages.where(type_id: wp_type.id).find_by(subject: m[:subject])\n"
            "      if wp.nil?\n"
            "        wp = WorkPackage.new(\n"
            "          project: project,\n"
            "          type: wp_type,\n"
            "          subject: m[:subject],\n"
            "          status: status,\n"
            "          priority: priority,\n"
            "          author: author\n"
            "        )\n"
            "      end\n"
            "      cf_values = {}\n"
            "      cf_values[cf_op_id] = m[:op_entity_id] if cf_op_id\n"
            f"      cf_values[cf_entity_type_id] = '{entity_type}' if cf_entity_type_id\n"
            "      wp.custom_field_values = cf_values if cf_values.any?\n"
            "      wp.save!\n"
            "      success += 1\n"
            "    rescue => e\n"
            "      failed += 1\n"
            '      errors << "#{m[:subject]}: #{e.message}"\n'
            "    end\n"
            "  end\n"
            "  { success: success, failed: failed, errors: errors.first(10) }\n"
            "rescue => e\n"
            "  { success: 0, failed: mappings.size, errors: [e.message] }\n"
            "end\n"
        )

        try:
            result = self._client.execute_json_query(script)
            if isinstance(result, dict):
                return result
            return {"success": 0, "failed": len(mappings), "errors": [f"Unexpected result: {result}"]}
        except Exception as e:
            return {"success": 0, "failed": len(mappings), "errors": [str(e)]}
