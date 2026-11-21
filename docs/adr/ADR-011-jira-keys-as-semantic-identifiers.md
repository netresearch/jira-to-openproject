# ADR-011: Jira keys become native OpenProject semantic work package identifiers

## Status

Accepted (2026-11-21)

## Context

OpenProject 17.9 has project-based semantic work package identifiers. The instance setting `work_packages_identifier` is `classic` (the default, the work package shows its numeric ID) or `semantic` (it shows `<project identifier>-<number>`). The data lives in `work_packages.identifier`, `work_packages.sequence_number`, `projects.wp_sequence_counter` and the table `work_package_semantic_aliases` (migration `20260330100000_create_work_package_semantic_ids.rb`; later `20260819090000` adds a cascading foreign key and `20260903120000` a non-unique `lower(identifier)` index). A lookup by display ID matches the `identifier` column or an alias row, case-sensitively, inside the caller's scope (`app/models/work_package/semantic_identifier/finder_methods.rb`).

The official OpenProject Jira importer (`app/workers/import/jira_create_project_work_packages_job.rb` at tag v17.9.8, lines 37-43 and 281-306) creates the work package with `skip_semantic_id_allocation: true`, then writes `identifier` = Jira key and `sequence_number` = the key's number with `update_columns`, upserts aliases from `alias_rows_for_sequence_number` (every project slug with the number) plus the `fromString` and `toString` of every `Key` changelog item, and sets the project's counter to the highest sequence number of the project. Its project job (`jira_create_project_job.rb`) creates the project with the Jira key as identifier and adds every earlier project key as a FriendlyId slug.

j2o keeps the key only in the `J2O Origin Key` custom field. Its project identifiers are the lower-case key (`project_identifier` in `src/application/components/project_migration.py`); a classic identifier must match `[a-z0-9\-_]+`, so j2o cannot give a project its upper-case key while the instance runs `classic`. A semantic project identifier must match `[A-Z][A-Z0-9_]*` and have at most 10 characters (`app/models/projects/identifier.rb`).

The switch to `semantic` in the administration (`Admin::Settings::WorkPackagesIdentifierController#switch_to_semantic`) is not a plain setting change. It runs `ProjectIdentifiers::ConvertProjectToSemanticService` for every pending project and sets the setting afterwards (`FinishSemanticConversionJob`). For each project the service:

1. replaces an identifier that is not valid semantic format with `previous_semantic_identifier` (the newest valid FriendlyId slug that no other project holds) or else with a suggestion derived from the project name;
2. clears `identifier` and `sequence_number` of every work package whose identifier is not exactly `<project identifier>-<sequence number>`;
3. numbers the work packages without a sequence number from the counter;
4. adds an alias for every project slug and sequence number.

Without further data the switch would therefore rename every j2o project from its name and renumber every work package.

The project owner decided on 2026-11-21 that the target instance switches to `semantic` after the last migration run, and that the importer writes the identifier data either way.

## Decision

### 1. A component of its own, behind its own switch

`semantic_identifiers` (`src/application/components/semantic_identifiers_migration.py`) runs right after `work_packages_skeleton`, so every work package of the run exists, level work packages included, and before anything else. It writes only when `migration.semantic_identifiers` is the boolean `true`; otherwise it reads nothing and writes nothing, and j2o writes exactly what it wrote before. The owner turns it on for this migration. A separate component instead of a skeleton step keeps the skeleton's creation path unchanged, needs the `Key` changelog (`expand=changelog`) only here, and can be repeated alone. Nothing goes into a mapping file: OpenProject holds the state, and every run reads it again.

### 2. The data of the official importer, per work package

Per work package the component writes `identifier` = Jira key and `sequence_number` = the key's number with `update_columns`, and adds the aliases `alias_rows_for_sequence_number` yields, every key of the issue's `Key` changelog, and the Jira key itself. The last one is redundant for an ordinary work package once its project carries the key; it is what keeps a level work package reachable (see 5). The Jira issue comes from the same unrestricted search as the other components, the work package from the work-package mapping; Rails confirms the work package's project and its `J2O Origin ID` before it writes.

A work package is refused, never overwritten, when it has another identifier or sequence number, when its key, an alias or its sequence number in the project belongs to another work package, or when it is in another project or carries another `J2O Origin ID`. A refusal or a failed batch makes the component unsuccessful. A repeat run writes nothing for a work package that already has its identifier and aliases and counts it as unchanged. A mapped issue that the search no longer returns (moved, deleted, or given a security level) is counted as `not_returned` and keeps no key; the README check before the switch counts every such work package on OpenProject.

### 3. The Jira key as project slug

For every migrated project the component adds the Jira key as a FriendlyId slug of the project, as the official importer does for earlier project keys. `previous_semantic_identifier` then returns the key, so the switch makes the key the project identifier, and step 2 of the conversion keeps every work package identifier. Before any work package is written, the project is refused when its key is not a valid semantic identifier (longer than 10 characters, or not `[A-Z][A-Z0-9_]*`), when another project holds the key as identifier or slug (case-insensitively, as the conversion compares), when the key is reserved, when the project already has another semantic identifier, or when `previous_semantic_identifier` does not return the key after the write.

### 4. Counters are raised, never lowered

A project's counter becomes the greatest of its current value, the highest sequence number in the project, and the highest number of any work package identifier or alias that starts with the project's key. The official importer sets it to the highest sequence number only. The wider rule also covers the keys of level work packages (they sit in a sub-project) and of issues that moved out of the project; otherwise the next new work package could get a key that an alias already resolves, and the lookup would match two work packages. A level sub-project's counter is raised to its highest sequence number.

### 5. Level work packages

A level work package (ADR-009) gets the Jira key as identifier and as alias, as the official importer would write it. Its sub-project's identifier (`app_level_10000`) is never a valid semantic identifier and never the key's prefix, so the switch renames the sub-project from its name and renumbers its work packages (conversion steps 1 and 2). After the switch a level work package displays the sub-project's identifier and number; its Jira key still resolves through the alias. A lookup runs inside the caller's visibility scope: a user who cannot see the sub-project gets the same "not found" for a level key as for a key that does not exist (measured on API v3, read for the HTML route; see below).

### 6. The instance setting stays with the operator

j2o never changes `work_packages_identifier`. The operator switches after the last run (README, *Post-Migration Cleanup*), with checks before and after.

### 7. `origin_*` columns: follow-up

OpenProject `dev` (migration `20261101120015`) adds `work_packages.origin_source`, `origin_id`, `origin_host` and `origin_url`, which the official importer writes next to the identifier (`jira_create_project_work_packages_job.rb` on `dev`, lines 400-407). 17.9.x has no such columns. When the target runs a release that has them, j2o should write them in the same step, detected at run time by `WorkPackage.column_names`. This is not implemented: on 17.9.7 only the absent branch could be checked, and the values the release expects (for example of `origin_source`) can change before it ships.

## Measured on the pilot

On the OpenProject 17.9.7 pilot, inside one transaction that was rolled back, with a throwaway project `zzsemt` (Jira key `ZZSEMT`), its level sub-project and two more projects, the component's Ruby (the strings of `semantic_identifiers_migration.py`):

- wrote `ZZSEMT-1`, `ZZSEMT-2` (with the alias `OLDZZ-4` from a `Key` change) and the level work package `ZZSEMT-10`;
- refused, with one reason each: a work package that already had `ZZSEMT-6`; one whose alias was another work package's identifier; one whose alias was another work package's alias row; one whose sequence number another work package of the project held; a row with another `J2O Origin ID`; a missing work package; the level work package sent for the parent project; the key `ZZSEMT` for a second project; and a key for a project that already had another semantic identifier;
- wrote nothing on a second run and left identifiers and aliases unchanged;
- raised the parent's counter to 20, the number of a `ZZSEMT-20` alias held by a work package in another project (a moved-out issue), above the parent's highest own sequence number 8 and the level key 10; the sub-project's counter became 10.

After `ConvertProjectToSemanticService` for the projects and the setting `semantic`: the project identifier was `ZZSEMT`; `ZZSEMT-1`, `ZZSEMT-2` and `OLDZZ-4` resolved to their work packages, which displayed `ZZSEMT-1` and `ZZSEMT-2`; the level work package displayed `ZSLL-11`, and `ZZSEMT-10` resolved to it and to nothing else; `ZZSEMT-20` still resolved only to the moved-out work package; the refused and unkeyed work packages got `ZZSEMT-21` to `ZZSEMT-25`, and a new work package `ZZSEMT-26`. A repeat write after the switch left the ordinary work packages unchanged and refused the level work package (another identifier). A member of the parent project only got HTTP 200 from `GET /api/v3/work_packages/ZZSEMT-1` and `OLDZZ-4`, and HTTP 404 with the same body for `ZZSEMT-10` and for the nonexistent `ZZSEMT-999`. Projects, work packages, aliases, slugs, custom values, users, jobs and the setting (`classic`) were unchanged afterwards, read again from a new process.

Read, not measured: the HTML route `/work_packages/<id>` answers "not found" through `authorize_on_work_package` before `redirect_to_complete_route` can name the project (`app/controllers/work_packages_controller.rb`, 17.9.7); the PDF mention renderers look up the identifier without scope and then check `visible?` (`work_package/pdf_export/generator/generator.rb`, `exports/pdf/common/markdown.rb`).

## Consequences

### Positive

- After the switch users see and find work packages by the Jira key they know, and old links with a key from before a move keep working.
- The data is the official importer's, so OpenProject's own conversion, rename and revert paths handle it.
- Nothing is overwritten: an identifier that is already set, or a key that another work package owns, stops the run instead of moving.

### Negative

- Level work packages do not display their Jira key after the switch; the key only resolves.
- A refused or not returned work package keeps no key and gets a new number from the switch; the run has to be clean before the switch.
- Runs after the switch are not supported: the conversion renumbered the level work packages, so a repeat run refuses each of them, and a work package created in semantic mode gets an allocated identifier, which the component then refuses.
- Earlier keys of a Jira project (a renamed project key) are not added as slugs; only issue `Key` changes become aliases.
- The switch derives the level sub-projects' semantic identifiers from their names.
- The project identifier changes from the lower-case key to the key at the switch; the lower-case form stays a slug, so old project URLs keep working.
