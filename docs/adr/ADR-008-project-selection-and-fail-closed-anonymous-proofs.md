# ADR-008: One source for the project selection; fail-closed anonymous proofs

## Status

Accepted (2026-11-20)

## Context

**Project selection.** Before [PR #467](https://github.com/netresearch/jira-to-openproject/pull/467), the CLI value of `--jira-project-filter` reached only `jira.projects`. The components `projects`, `versions`, `affects_versions` and `agile_boards` read `migration.jira_project_filter` instead. A live run limited to one project created all 245 Jira projects and wrote boards into five other projects.

**Anonymous proofs.** Two places decide whether Jira content is public by reading it without credentials:

- `reporting` publishes a filter or dashboard only if an anonymous read returns it (`public_report` in `src/infrastructure/jira/jira_public_reporting.py`).
- The public-comment selection copies a comment natively only if an anonymous read returns the identical body (`select_comments` in `src/infrastructure/jira/comment_audience.py`; see [ADR-007](ADR-007-protected-comments-and-local-acceptance.md)).

Both proofs used sessions with `trust_env = False`, so they did not see `REQUESTS_CA_BUNDLE`. Against a Jira behind a private CA they failed with an SSL error. Jira Data Center also answers an anonymous read of a filter shared only with logged-in users with HTTP 400. In a live run, that 400 aborted the whole `reporting` component.

## Decision

### One source for the project selection

`config.selected_jira_projects()` is the only reader of the project selection ([PR #467](https://github.com/netresearch/jira-to-openproject/pull/467)). The first non-empty source wins: the `--jira-project-filter` CLI value, then `jira.projects`, then `migration.jira_project_filter`. Strings are comma-separated lists. Keys are upper-cased and de-duplicated, and an empty list selects every project.

All 14 project-scoped components use this function. A static test in `tests/unit/test_project_filter.py` fails if any module other than the config module reads either key directly.

### Anonymous proofs fail closed

A record is treated as public only when the anonymous read positively confirms it. Every other outcome holds the record back or raises; none of them publishes.

- **TLS** ([PR #464](https://github.com/netresearch/jira-to-openproject/pull/464)): both proofs verify TLS with the CA bundle the Jira client's own session uses (`anonymous_verify`). If there is no bundle, they use the default verification. This path never turns verification off.
- **Filters and dashboards** (`public_report`, [PR #470](https://github.com/netresearch/jira-to-openproject/pull/470)): a 400, 401, 403 or 404, or a redirect, holds the record back as not public. Other error statuses, among them 5xx, still raise. A record is also held back when the URL is not plain HTTPS, when Jira does not identify the caller as `anonymous` in `X-AUSERNAME`, when the response is not JSON, or when the returned ID differs.
- **Comments** (`select_comments`): a 401, 403 or 404, a redirect, or a reply that Jira does not mark as `anonymous` yields no public comment. Other error statuses, a 400 included, raise. Each page, authenticated or anonymous, must be internally consistent (`startAt`, a stable total, unique comment IDs). A comment is copied only if the anonymous read returns it as eligible with a body identical to the authenticated read.

## Consequences

### Positive

- A run limited to some projects touches only those projects, whichever way the limit is given.
- A new component cannot quietly read the selection keys on its own; the static test catches it.
- A Jira behind a private CA works for both proofs without weakening TLS verification.
- A filter Jira refuses anonymously is held back instead of aborting `reporting`.
- An unclear answer from Jira never makes content public.

### Negative

- Content that cannot be proven public anonymously is held back, for example everything on a Jira that is reachable only over plain HTTP.
- The two proofs still treat a 400 differently: filters and dashboards hold the record back, comments raise.
- The CLI value and two configuration keys still feed the selection. Their precedence has to be known when more than one is set.
