# Operator archive of restricted Jira issues

`j2o-restricted` records who can read each Jira issue that has an issue security level, and keeps every issue that cannot become an ordinary work package of its level in an age-encrypted archive for the operator. It only reads Jira; it makes no OpenProject request. Issues with a level stay out of the standard migration as before. The decisions behind it are in [ADR-009](adr/ADR-009-security-levels-as-private-sub-projects.md).

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

- `report.json` (mode 600) is the shared run report: counts per project and level (issues, static readers, each class, single-user grants that name the migration account, issues where the account was not counted) and the keys of archived issues with their class and the number of extra and missing readers. It holds no reader names.
- `restricted-issues.age` is encrypted to the operator key. It holds every `per_issue` and `unexplained` issue in full (all fields, the complete changelog, all comments and worklogs, attachment metadata), with its readers, and each level's grants and static readers. Attachment contents are not included.

Restore the archive on a trusted machine into a new private file:

```sh
uv run --project /path/to/j2o j2o-restricted restore --identity operator.agekey \
  --archive restricted-issues.age --output restricted-issues.json
```

## Repeat runs and known limits

Grants and memberships can change until cutover. Repeat the inventory at cutover, in a new directory or with new file names: archive and report are never overwritten. A failed run is repeated the same way, in a new directory, even when it already wrote the archive.

- An issue whose assignee, reporter, reader field or grants change during the scan can be misclassified, because its fields are read before its readers. Run the final inventory during the cutover freeze.
- The archive holds the comments visible to the migration account. With the temporary grant and the account's admin roles this is expected to be all comments; it has not been measured.
