# ADR-007: Protected comments stay out of native journals; acceptance runs locally

## Status

Accepted (2026-11-20)

## Context

Jira comments can be restricted to a group or a project role. A native OpenProject journal cannot keep such a reader audience; this is the reason legacy journal replay is rejected since [PR #387](https://github.com/netresearch/jira-to-openproject/pull/387) (see the 2026-11-19 update of [ADR-001](ADR-001-two-phase-work-package-migration.md#update-2026-11-19-field-change-history)).

The cutover needs evidence that each reader keeps exactly the comments they could read in Jira.

## Decision

### Restricted comments are not copied natively

The standard import does not write restricted comments as OpenProject journals. `work_packages_content` takes its comments from `select_comments` in `src/infrastructure/jira/comment_audience.py`, which is stricter than that:

- Unless `J2O_COPY_PUBLIC_COMMENTS=1` is set, it copies no comment natively.
- With the switch set, it copies only comments that have no visibility restriction or internal flag and whose identical body an anonymous Jira read over HTTPS returns.

Every other comment is counted as held at the source.

### Reader-encrypted archives carry the protected comments

`j2o-comments` stores reader-encrypted copies in one attachment, `jira-protected-comments.j2o`, per mapped work package with comments. Each reader decrypts exactly the view captured with their own Jira account. OpenProject backups, project access and the OpenProject admin account alone cannot decrypt it. The procedure is in [`docs/protected-comments.md`](../protected-comments.md).

### Acceptance runs locally with copies of real comments

On 2026-11-19 the project owner decided how the cutover permission acceptance runs:

- locally, with copies of real comments and test accounts;
- no colleagues take part;
- no shared HTTPS target is used.

[PR #466](https://github.com/netresearch/jira-to-openproject/pull/466) implements this as an opt-in integration test:

1. `company_comment_sample.py` selects cases from the real Jira with GET requests only. It writes a private fixture outside the repository.
2. The test starts a local Jira Software 9.12.3 and OpenProject 17.9.7 behind a private CA on `127.0.0.1`.
3. It recreates each case in its own project, with one authorized and one unauthorized test account. The cases are ordinary comments, a group restriction, a project-role restriction and an issue security level.
4. It runs `j2o migrate --profile full` with `J2O_COPY_PUBLIC_COMMENTS=0` and publishes each project's archive with `j2o-comments`.
5. It compares what each account reads in both systems, and it checks anonymous access and the ordinary activity stream.

The test is skipped unless `J2O_COMPANY_COMMENT_FIXTURE` and `J2O_ACCEPT_STACK` are set, so CI does not run it.

`docs/protected-comments.md` ("Migrate and verify") still asks representative users to read back their own views before Jira is switched off. Since the owner decided that no colleagues take part, that step does not apply to this cutover.

How issues with a security level are handled is still being designed, and this ADR does not decide it. The security-level case of the acceptance test fails by design, because such issues are not migrated as work packages.

## Consequences

### Positive

- No restricted comment becomes readable to a wider audience through a native journal.
- The acceptance checks real comment bodies and visibility rules without exposing company content. The fixture stays outside the repository, and the runner prints counts only.
- The acceptance needs no colleague's time and no shared server.
- In the run reported in PR #466, the authorized and unauthorized accounts read 1 and 0 comments (ordinary), 2 and 1 (group restriction) and 2 and 1 (project-role restriction), the same in Jira and OpenProject. The unauthorized accounts saw no restricted comment, and anonymous access was refused.

### Negative

- Protected comments do not appear in OpenProject search or the native activity stream. Readers need their personal age key and the `j2o-comments read` command.
- By default no comment is copied natively, not even a public one.
- Real users do not read back their own views before Jira is switched off. The acceptance relies on test accounts that mirror the permission cases.
- The loader cannot keep the original comment author and date. The copies carry the loader account and the load time.
- The security-level case is granted to a synthetic group, not to the real level's grants.
