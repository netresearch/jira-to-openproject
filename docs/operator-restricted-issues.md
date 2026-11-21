# Operator archive of restricted Jira issues

`j2o-restricted` records who can read each Jira issue that has an issue security level, and keeps every issue that cannot become an ordinary work package of its level in an age-encrypted archive for the operator. `inventory` only reads Jira; it makes no OpenProject request. `admins` reads the archive and OpenProject and changes nothing. Issues with a level stay out of the standard migration unless `security_level_work_packages` is switched on (see [Level work packages](#level-work-packages)). The decisions behind it are in [ADR-009](adr/ADR-009-security-levels-as-private-sub-projects.md).

## What it computes

For each project with an issue security scheme:

1. The levels come from `/rest/api/2/project/{KEY}/issuesecuritylevelscheme`, their grants from the project-config page `/plugins/servlet/project-config/{KEY}/issuesecurity` (Jira 9 has no REST endpoint for them). The page was read with a Jira administrator account; other accounts were not tried. A login or WebSudo page instead of the grant table stops the run.
2. **Static readers** of a level: the members of its group and project-role grants, the project lead and its single users, intersected with the users who have Browse on the project.
3. **Effective readers** of each issue: `/rest/api/2/user/permission/search?permissions=BROWSE&issueKey=…`.
4. Each issue gets one class:
   - `static`: the effective readers equal the static readers.
   - `per_issue`: additional readers, each named by the issue's assignee, reporter or user-field grant.
   - `unexplained`: a reader no grant explains, a static reader missing from the effective readers, or a level with a grant label the command does not know (for example "Current user", or a label in another language). The grants of such a level are not resolved, so the report shows 0 static readers for it. Such issues are archived and the run continues.

The migration account's temporary "Single user" grant (ADR-009, decision 3) is not counted. The account is removed from an issue's effective readers only when the level has a "Single user" grant naming it and no other grant explains it. On a level without such a grant the account stays a reader, and the issue is `unexplained` unless another grant explains the account. Pass its Jira user name with `--migration-account`; the default is `J2O_JIRA_USERNAME`, the configured Jira user. The account must exist in Jira and must own the inventory token: the command compares it with `/rest/api/2/myself` and stops on a mismatch.

## Prerequisites

Jira returns only the issues the token user can read. Before each run:

- The migration account has Browse Projects on every project in scope.
- The temporary "Single user" grant for the migration account is on every security level of every project in scope.
- Without `--project`, the instance total of `level IS NOT EMPTY` checks coverage only for issues visible to the token; an issue the token cannot read is missing from both the total and the scan.

Remove the temporary grants after the final scan.

## Run

The inventory token belongs to the migration account. Set `J2O_ARCHIVE_INVENTORY_TOKEN` through your secret store, and `J2O_ARCHIVE_CA_BUNDLE` when Jira uses a private CA. Run the command in a private directory; archive and report must be plain file names in it and must not exist yet.

```sh
mkdir -m 700 ~/restricted-inventory && cd ~/restricted-inventory
uv run --project /path/to/j2o j2o-comments keygen --identity operator.agekey
uv run --project /path/to/j2o j2o-restricted inventory --jira https://jira.example.org \
  --identity operator.agekey --archive restricted-issues.age --report report.json
```

Without `--project` every project is scanned, and the total of `level IS NOT EMPTY` must equal the issues found. Repeat `--project KEY` to limit the scan.

- `report.json` (mode 600) is the shared run report: counts per project and level (issues, static readers, each class, single-user grants that name the migration account, issues where the account was not counted), the keys of each level's `static` issues (`static_keys`), and the keys of archived issues with their class and the number of extra and missing readers. It holds no reader names.
- `restricted-issues.age` is encrypted to the operator key. It holds every `per_issue` and `unexplained` issue in full (all fields, the complete changelog, the comments visible to the migration account, all worklogs, attachment metadata), with its readers, and each level's grants and static readers. Attachment contents are not included.

Restore the archive on a trusted machine into a new private file:

```sh
uv run --project /path/to/j2o j2o-restricted restore --identity operator.agekey \
  --archive restricted-issues.age --output restricted-issues.json
```

## Edition and administrator report

OpenProject administrators see every work package, so they will see the work packages of every level sub-project. The import does not stop for that (ADR-009, decision 4); `admins` lists them before any level sub-project is created. Run it from the j2o working directory of the migration: the Rails console connection comes from its `config/config.yaml`, and `--user-mapping` names the migration's `var/data/user_mapping.json`. Set `J2O_OPENPROJECT_API_KEY` through your secret store. The report is written to the working directory and must not exist yet; the checkout ignores `restricted-admins*.json`, so keep that name.

```sh
cd /path/to/j2o
uv run j2o-restricted admins --identity ~/restricted-inventory/operator.agekey \
  --archive ~/restricted-inventory/restricted-issues.age --user-mapping var/data/user_mapping.json \
  --openproject https://openproject.example.org --report restricted-admins.json
```

- **Edition.** Rails answers `EnterpriseToken.allows_to?` for `work_package_sharing` and `internal_comments`; `GET /api/v3/configuration` answers `availableFeatures`. The edition is `enterprise` only when both allow `work_package_sharing`; a disagreement is logged and means `community`. A failed or malformed answer, or a Rails console and API URL that report different host names, stops the command; so does a user mapping without a single usable entry. The report keeps both answers (feature names only).
- **Administrators.** Every non-deleted administrator, with its status (`active`, `locked`, `registered`, `invited`). An administrator maps to the Jira users whose user-mapping entry names its OpenProject ID; user names are compared case-insensitively. Per level, `restricted-admins.json` (mode 600) lists the logins of the administrators none of whose Jira users is a static reader of the level. `unmapped_admins` lists administrators without a Jira user; an entry restored from OpenProject (`restored_from_op`) guesses the Jira name from the login and does not count as a mapping. A level with an unsupported grant has no computed readers (`readers_computed: false`) and lists nobody; its issues are archived anyway. The report names administrators only, never Jira readers.

## Level sub-projects

The `security_level_projects` component creates, for each Jira project and level with at least one issue that will go to OpenProject, one private sub-project under the project's OpenProject project (ADR-009, step 3 of the order of work). Static issues count on both editions, `per_issue` issues only when the administrator report says `enterprise`; `unexplained` issues never count. The component reads only the shared `report.json` and the administrator report, never the archive. It is in no profile yet; run it after `projects`:

```yaml
migration:
  security_level_report: /home/me/restricted-inventory/report.json
  security_level_admin_report: /path/to/j2o/restricted-admins.json
```

```sh
uv run j2o migrate --components security_level_projects --no-confirm
```

- **Precondition.** Both files must exist, have their format and name the same `archive_sha256`; otherwise the component stops before any OpenProject request. The content of the administrator report never stops it; only its `edition` is used. That edition is the one detected when `admins` ran: run `admins` again after a licence change.
- **Identifier and name.** The identifier is the Jira project's identifier (lower case, `-` for every other character, `p-` before a leading digit) followed by `_level_` and the level ID, for example `app_level_10000`. A key-derived identifier never contains `_`, so it cannot equal one; an identifier longer than 100 characters is refused. The name is the parent's current OpenProject name, read from OpenProject in the same request that looks for the sub-project, with the level name in parentheses, for example `App (Management)`. An identifier or name that belongs to an entry of the project mapping is refused.
- **Parent.** The parent is the OpenProject project of the Jira project in `project_mapping.json`. When that file has no entry with an OpenProject ID for one of the Jira projects, the component reads the project entries of the J2O provenance registry once, as `ProjectMigration.restore_mapping_from_openproject()` does, without rewriting `project_mapping.json`; restored names count as known names. A level whose parent is found in neither, or whose parent no longer exists in OpenProject, is refused.
- **Properties.** The sub-project is created with `public: false`, its parent's types (a variant the parent owns falls back to the type's base variant) and the modules `work_package_tracking` and `wiki`, through `Project.new` rather than `Projects::CreateService`, which would add the creating user as a member. No member is added; OpenProject 17.9.7 copies no memberships from a parent project. It carries no J2O Origin fields, so lookups by Jira project key keep finding only the parent.
- **Mapping.** `security_level_project_mapping.json` holds one entry per `<Jira key>:<level ID>` with the OpenProject project ID and identifier. `project_mapping.json` stays one project per Jira project.
- **Repeat runs.** An existing project with the identifier is accepted only when it is private, has no membership row (locked users and work package shares included) and has the expected parent. Otherwise that level is refused and reported, and the project is not changed. Work packages go into the sub-projects only with the level switch on (see [Level work packages](#level-work-packages)).

## Level work packages

With `migration.security_level_work_packages: true`, `work_packages_skeleton` also creates the work packages of the issues of every level that has a sub-project (ADR-009, step 4 of the order of work). `work_packages_content` does not write their content yet. The switch is off by default, and only the boolean `true` turns it on. Leave it off until the dependent components (attachments, versions, boards, relations, watchers, time entries) and the late access grant are in place: until then a level work package has no attachments, watchers, relations or time entries, and no member can read it.

```yaml
migration:
  security_level_report: /home/me/restricted-inventory/report.json
  security_level_admin_report: /path/to/j2o/restricted-admins.json
  security_level_work_packages: true
```

- **Selection.** Per Jira project, one search `project = KEY AND level IS NOT EMPTY`, split by the level ID each result carries (`fields.security.id`); a search per level fails with HTTP 400 for a level of which the migration account sees no issue. The report lists the keys of each level's `static` issues and of its archived issues. `static` issues go on both editions, `per_issue` issues only when the administrator report says `enterprise`, `unexplained` issues never.
- **Fail closed.** A search result without a level ID is refused. A level is refused as a whole when the search does not return exactly the issue keys the report lists for it (an issue created, deleted or moved since the inventory refuses its level; run the inventory again during the cutover freeze), when an archived issue has another level in the search than in the report, or when its sub-project entry names another project, level or identifier. An issue of a level without a sub-project entry is created nowhere (`unmapped`). Before the first work package goes into a sub-project, the skeleton reads it again: it must exist with the mapped ID, be private, have no membership row and have the mapped parent. An issue that already has a work package in `work_package_mapping.json`, or one in another sub-project, is refused. With the switch on, a missing or mismatched report stops the component before any write. Refusals make the component unsuccessful; `details.security_level` counts them.
- **Mapping.** Level work packages are recorded in `security_level_work_package_mapping.json`, never in `work_package_mapping.json`, so the components that read the work-package mapping keep leaving them alone until they are adapted. An entry carries `jira_key`, `openproject_id` and `project_key` as usual, plus the sub-project's `openproject_project_id`, `security_level_id`, `security_level_project` (`<Jira key>:<level ID>`) and `security_class`.
- **Limits.** `J2O_MAX_ISSUES` does not limit the level search, because the selection has to see every issue of a level. Equal counts do not prove the same issues: an issue removed and another added since the inventory pass the check, so repeat the inventory at cutover. The field history (ADR-004) still skips level issues. With the switch on, an issue that loses its level after its work package was created is not created again in the parent project, and its work package stays in the sub-project; with the switch off it would get a second work package in the parent project.

## Repeat runs and known limits

Grants and memberships can change until cutover. Repeat the inventory at cutover, in a new directory or with new file names: archive and report are never overwritten. A failed run is repeated the same way, in a new directory, even when it already wrote the archive.

- An issue whose assignee, reporter, reader field or grants change during the scan can be misclassified, because its fields are read before its readers. Run the final inventory during the cutover freeze.
- The archive holds the comments visible to the migration account. With the temporary grant and the account's admin roles this is expected to be all comments; it has not been measured.
