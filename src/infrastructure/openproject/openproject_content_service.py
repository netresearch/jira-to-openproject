"""Project-attached content helpers for the OpenProject Rails console.

Phase 2x of ADR-002 continues the openproject_client.py god-class
decomposition by collecting *project-attached content* helpers — i.e.
saved queries and wiki pages: content owned by a project but not by
a single work package.

The service owns:

* **Saved queries** — ``create_or_update_query`` (Query
  find-or-initialize on ``(name, project)`` with column / filter /
  sort / group_by / hierarchy options, public flag, and admin user
  fallback).
* **Wiki pages** — ``create_or_update_wiki_page`` (Wiki page
  find-or-initialize on ``(wiki, title)`` within the project's wiki,
  text/content body assignment, admin author fallback, and journal
  touch on update).

``OpenProjectClient`` exposes the service via ``self.content`` and
keeps thin delegators for the same method names so existing call sites
(``reporting_migration``, ``agile_board_migration``) work unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectContentService:
    """Project-attached content helpers (saved queries, wiki pages)."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── saved queries ────────────────────────────────────────────────────

    def create_or_update_query(
        self,
        *,
        name: str,
        description: str | None = None,
        project_id: int | None = None,
        is_public: bool = True,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update an OpenProject query (saved filter).

        All user-supplied strings (``name``, ``description``, the
        contents of ``options``) are bundled into a Python dict and
        round-tripped through ``json.dumps`` into a single-quoted Ruby
        heredoc (``<<'JSON_DATA'``), so no value is ever interpolated
        into Ruby source — ``JSON.parse`` reconstructs them as data on
        the Ruby side.

        ``project_id`` is coerced via ``int()`` (when not ``None``)
        before serialisation so a stray non-numeric value produces a
        ``ValueError`` at the Python boundary instead of leaking
        through to Ruby.
        """
        # Defensive ``int()`` coercion at the Python boundary — see
        # module docstring. Ruby additionally re-coerces via ``.to_i``.
        coerced_project_id: int | None = int(project_id) if project_id is not None else None

        payload = {
            "name": name,
            "description": description,
            "project_id": coerced_project_id,
            "is_public": bool(is_public),
            "options": options or {},
        }

        # ``ensure_ascii=False`` emits UTF-8 directly (avoids ``\uXXXX``
        # escapes that Ruby misinterprets). The single-quoted heredoc
        # tag (``<<'JSON_DATA'``) suppresses Ruby interpolation, so
        # emitting the JSON payload directly here is safe — ``JSON.parse``
        # on the Ruby side treats it as data, not code.
        payload_json = json.dumps(payload, ensure_ascii=False)
        # Drop ``.to_json`` from the terminal expression —
        # ``execute_query_to_json_file`` already wraps the script tail
        # in ``.as_json`` on the Ruby side, so an explicit ``.to_json``
        # would double-encode and Python would receive a JSON string
        # instead of a parsed dict.
        script = f"""
        require 'json'
        input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

        begin
          project = input['project_id'] ? Project.find_by(id: input['project_id'].to_i) : nil
          # Pick an owning user — the previous version had a redundant
          # ``User.respond_to?(:admin)`` ternary that fell straight
          # through to an unconditional ``User.admin.first`` on the
          # next line, defeating the guard. Try the most-specific
          # source first (``User.admin`` scope when available), then
          # the column-level filter, then any active user.
          user = nil
          user ||= User.admin.first if User.respond_to?(:admin)
          user ||= User.where(admin: true).first
          user ||= User.active.first

          if user.nil?
            result = {{ success: false, error: 'no available user to own query' }}
          else
            query = Query.find_or_initialize_by(name: input['name'], project: project)
            query.user ||= user

            # Apply the description if the model exposes one — older
            # OpenProject versions don't. ``input['description']`` is
            # ``nil`` when the caller didn't pass one, in which case
            # we leave the existing description untouched.
            description = input['description']
            if !description.nil? && query.respond_to?(:description=)
              query.description = description.to_s
            end

            is_public = !!input['is_public']
            if query.respond_to?(:public=)
              query.public = is_public
            elsif query.respond_to?(:write_attribute) && query.has_attribute?(:public)
              query.write_attribute(:public, is_public)
            end

            filters = input.dig('options', 'filters')
            query.filters = Array(filters) if filters && query.respond_to?(:filters=)

            columns = input.dig('options', 'columns')
            query.column_names = Array(columns) if columns && query.respond_to?(:column_names=)

            sort = input.dig('options', 'sort')
            query.sort_criteria = Array(sort) if sort && query.respond_to?(:sort_criteria=)

            group_by = input.dig('options', 'group_by')
            query.group_by = group_by if group_by && query.respond_to?(:group_by=)

            hierarchies = input.dig('options', 'show_hierarchies')
            if !hierarchies.nil? && query.respond_to?(:show_hierarchies=)
              query.show_hierarchies = hierarchies
            end

            if query.respond_to?(:include_subprojects=) && query.include_subprojects.nil?
              query.include_subprojects = false
            end

            created = query.new_record?
            changed = query.changed?
            query.save! if created || changed

            result = {{
              success: true,
              id: query.id,
              created: created,
              updated: changed || created
            }}
          end
        rescue => e
          result = {{ success: false, error: e.message }}
        end

        result
        """

        result = self._client.execute_query_to_json_file(script, timeout=120)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}

    # ── wiki pages ───────────────────────────────────────────────────────

    def create_or_update_wiki_page(
        self,
        *,
        project_id: int,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        """Create or update a Wiki page within a project.

        All user-supplied strings (``title``, ``content``) are bundled
        into a Python dict and round-tripped through ``json.dumps``
        into a single-quoted Ruby heredoc (``<<'JSON_DATA'``), so no
        value is ever interpolated into Ruby source — ``JSON.parse``
        reconstructs them as data on the Ruby side.

        ``project_id`` is coerced via ``int()`` before serialisation
        so a stray non-numeric value produces a ``ValueError`` at the
        Python boundary instead of leaking through to Ruby.
        """
        # Defensive ``int()`` coercion at the Python boundary — see
        # docstring. Ruby additionally indexes a JSON-decoded integer.
        payload = {
            "project_id": int(project_id),
            "title": title,
            "content": content,
        }
        # ``ensure_ascii=False`` emits UTF-8 directly (avoids ``\uXXXX``
        # escapes that Ruby misinterprets). Single-quoted heredoc tag
        # disables interpolation; ``JSON.parse`` reconstructs the dict
        # on the Ruby side.
        payload_json = json.dumps(payload, ensure_ascii=False)

        # Restructure the original "unless project; return {...}.to_json; end"
        # branch into a single trailing expression. Two reasons:
        #   1. ``return`` at the top level of a Rails-runner script
        #      raises ``LocalJumpError`` once the runner wraps the
        #      script in ``(...).as_json``.
        #   2. The trailing ``.to_json`` would double-encode through
        #      ``execute_query_to_json_file`` (the runner already wraps
        #      the script tail with ``.as_json``).
        script = f"""
        require 'json'
        input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

        project = Project.find_by(id: input['project_id'])
        if project.nil?
          {{ success: false, error: 'project not found' }}
        else
          begin
            wiki = project.wiki || project.create_wiki(start_page: 'Home')
            page = wiki.pages.where(title: input['title']).first_or_initialize
            author = User.admin.first || User.active.first || User.first
            raise 'no available author for wiki content' unless author

            page.wiki ||= wiki if page.respond_to?(:wiki=)
            page.author ||= author if page.respond_to?(:author=)

            body_text = input['content'].to_s

            if page.respond_to?(:text=)
              page.text = body_text
            elsif page.respond_to?(:content=)
              page.content = body_text
            else
              raise 'wiki page entity does not support text assignment'
            end

            created = page.new_record?
            changed = page.changed?
            page.save!

            # Ensure timestamps/ journaling persists author for updates
            page.touch if !created && changed && page.respond_to?(:touch)

            {{
              success: true,
              id: page.id,
              updated_on: page.updated_at
            }}
          rescue => e
            {{
              success: false,
              error: e.message
            }}
          end
        end
        """

        result = self._client.execute_query_to_json_file(script, timeout=120)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}
