# ADR-009: Issues with a Jira security level become private OpenProject sub-projects

## Status

Accepted (2026-11-20)

## Context

Jira hides an issue with an issue security level from everyone whom the level does not grant. A user reads such an issue only when he has Browse Projects in the project **and** one of the level's grants matches him. Until now j2o does not migrate these issues at all: the work-package components select `level IS EMPTY` (`src/application/components/issue_security.py`) or exclude restricted issues through the work-package mapping, and [MIGRATION_COMPONENTS.md](../MIGRATION_COMPONENTS.md) documents the exclusion. No archive keeps them.

Jira grants a level to two kinds of principals:

- **Static grants** name a fixed set of users: group, project role, project lead, single user.
- **Per-issue grants** depend on the issue: current assignee, reporter, and a user custom field (in this Jira a multi-user picker for extra readers).

A measurement against the production Jira (Server 9.12.3, read-only) found these facts the design rests on:

- Jira 9.12.3 has no REST endpoint for the grants of a level. The project-config page `/plugins/servlet/project-config/{KEY}/issuesecurity` lists them, single users by display name.
- `/rest/api/2/user/viewissue/search` ignores the level: for one restricted issue it returned the whole project Browse set (23 users) instead of the two readers. `/rest/api/2/user/permission/search?permissions=BROWSE&issueKey=…` honours it.
- A level grant does not imply Browse: an active user named by a single-user grant had no Browse on the project and could not read the issues. Granting OpenProject membership from the level grants alone would widen access.
- Jira answered the JQL `level = <id>` with HTTP 400 ("A value with ID … does not exist for the field 'level'") for a level of which the token user sees no issue. The inventory therefore runs one `level IS NOT EMPTY` search per project and partitions the result by the issue's `security.id`.

Jira admin rights do not bypass issue security, so a search returns only the restricted issues the token user can read.

OpenProject 17.9.7 (the target version named in the README) offers these mechanisms. Source references are to the opf/openproject tag v17.9.7:

- A non-admin sees the work packages of projects he is a member of, and work packages shared with him (`app/models/work_packages/scopes/allowed_to.rb:64-83`). Project visibility has no inheritance from the parent project; non-members see only public projects (`app/models/projects/scopes/allowed_to.rb:104`). A private sub-project is visible only to its own members.
- Administrators see every project and work package (`app/models/work_packages/scopes/allowed_to.rb:45-46`; ["Administrators have full access to all settings and all projects"](https://www.openproject.org/docs/system-admin-guide/users-permissions/roles-permissions/)).
- Sharing a work package is an Enterprise add-on. The user gets the role "Work Package Viewer" and "is not automatically a member of the whole project" ([Share work packages](https://www.openproject.org/docs/user-guide/work-packages/share-work-packages/)). In code the Enterprise token gates the UI; the share contracts and the visibility scope do not check it (`app/contracts/shares/base_contract.rb:35-80`, `app/models/work_packages/scopes/allowed_to.rb:64-83`).
- Internal comments are an Enterprise add-on, visible per role permission and by default only to the Project admin role ([Activity: internal comments](https://www.openproject.org/docs/user-guide/activity/); `app/contracts/work_packages/create_note_contract.rb:45`).
- `EnterpriseToken.allows_to?(feature)` decides availability in Rails (`app/models/enterprise_token.rb:56-58`); `GET /api/v3/configuration` exposes the same token set as `availableFeatures` (`lib/api/v3/configuration/configuration_representer.rb:106-109`).

## Decision

### 1. One private sub-project per Jira project and level

An issue with a security level becomes an ordinary work package in a private OpenProject sub-project of its project's OpenProject project, one sub-project per (Jira project, level). The members of the sub-project are exactly the level's static readers: the users of its group, project-role, project-lead and single-user grants, **intersected with the users who have Browse on the Jira project**. Per-user memberships are assigned, not group mirrors. A level whose grants include a type without a fixed membership other than the per-issue types (for example "current user") is not migrated.

### 2. Community and Enterprise; edition detected at run time

The importer supports OpenProject Community and Enterprise. It detects the edition at run time with `EnterpriseToken.allows_to?(:work_package_sharing)` and cross-checks it with `availableFeatures` from `GET /api/v3/configuration`. If the two disagree, the edition is treated as Community.

An issue whose readers equal the level's static readers is migrated on both editions. An issue with **per-issue readers** (an assignee, reporter or extra-reader field value that is not a static reader) is handled by edition:

- **Enterprise:** migrated into its sub-project and shared (Work Package Viewer) with exactly its extra readers. Because the share contracts do not check the token, the importer gates on the detected edition itself.
- **Community:** not migrated to OpenProject (see 5).

An issue whose readers the grants do not explain (a reader outside every grant, or a static reader Jira does not list) is not migrated on either edition.

### 3. The migration account and its temporary grant

So that the inventory sees every restricted issue, the owner adds the migration account (the Jira token user) as a "Single user" grant to every security level for the duration of the migration and removes it afterwards.

The reader computation does not count this grant. The migration account counts as a reader of an issue only when another grant explains it: group, project role, project lead, another single-user grant of the level, assignee, reporter or the extra-reader field. Concretely:

- A single-user grant that resolves to the migration account is never a static grant.
- Where only that grant explains the account, the account is removed from the issue's effective readers.

A level holds at most one single-user grant per user, so a single-user grant to the migration account that existed **before** the migration cannot be told apart from the temporary one and is not counted either. Such a grant narrows the readers by one account; it never widens them. The run report counts, per level, the single-user grants that named the migration account, so a pre-existing grant is visible before the owner adds the temporary ones. The account name is configurable (`j2o-restricted inventory --migration-account`); the default is the configured Jira user (`J2O_JIRA_USERNAME`, the source of `jira.username`). The account must exist in Jira.

### 4. OpenProject administrators do not stop the import

OpenProject administrators see every work package. The import does not stop for that. The run report lists, per level, the OpenProject administrators who are not Jira readers of the level (implemented with the edition detection). Rationale: a Jira administrator can reach these issues too if he wants, by adding himself to a grant, impersonating or scripting; the difference is only that OpenProject shows them without a deliberate action.

### 5. Operator archive for issues that do not go to OpenProject

Issues with per-issue readers on Community, and issues with unexplained readers on any edition, go to a new age-encrypted operator archive for restricted issues, readable with the operator key: `j2o-restricted` (`src/restricted_issues.py`, [docs/operator-restricted-issues.md](../operator-restricted-issues.md)). `j2o-history` is not extended. Each archived issue holds the full Jira issue (all fields, the complete changelog, all comments and worklogs, attachment metadata), its level, its effective readers and its extra and missing readers. Attachment contents are not in the archive. With an Enterprise licence later, the migration can import the per-issue issues from the archive.

The shared run report carries counts per project and level and the keys of archived issues, never reader names.

### 6. Decided by Claude, not by the owner

- **Internal comments are not used** to imitate Jira visibility. They are visible per role permission, not per user or Jira group, so they are no exact mapping of a level or of a restricted comment. The comments of a restricted issue are protected by the work package's project.
- **Version sharing for sprints** used by work packages in a sub-project is decided in a later pull request.

### Order of work

1. Inventory, readers check and operator archive, without OpenProject writes (this ADR's first pull request). Restricted issues stay unmigrated.
2. Edition detection and the administrator report.
3. Memberless private sub-projects with a separate mapping.
4. Work packages of level issues in their sub-project.
5. Dependent components (attachments, versions, boards, relations, watchers, time entries).
6. Late access grant: memberships, and shares on Enterprise, read back after writing.

## Consequences

### Positive

- Restricted issues reach OpenProject with exactly their Jira readers instead of being left behind.
- Membership follows the grants intersected with Browse, so a grant to a user without Browse does not widen access.
- The temporary grant that makes the inventory complete does not leak into the readers.
- Nothing is silently dropped: every issue that does not go to OpenProject is in the archive and counted in the report.

### Negative

- The grants come from an admin UI page, not an API. The page is parsed for one table; any other page (a login or WebSudo interstitial) or an unknown grant label fails closed. Its labels depend on the token user's language (English is supported).
- Single users are named by display name on the page; a display name that matches more than one Jira user stops the run.
- Grants and memberships can change until cutover; the inventory has to be repeated at cutover.
- A pre-existing single-user grant to the migration account is not counted (see 3).
- OpenProject administrators can read restricted work packages without a deliberate action (see 4).
- On Community, per-issue issues are readable only from the operator archive, and the archive holds no attachment contents.
- Per-issue readers on Enterprise see only their shared work packages, not the project's versions, boards or time entries.
