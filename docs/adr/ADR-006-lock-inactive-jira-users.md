# ADR-006: Inactive Jira users become locked OpenProject users

## Status

Accepted (2026-11-20)

## Context

Before [PR #473](https://github.com/netresearch/jira-to-openproject/pull/473), `users` created every OpenProject user as active. In a live run 366 of 447 Jira users were inactive. An inactive user who was still in a Jira role group could read the project's work packages in OpenProject.

Several Jira identities can end up on one OpenProject account: a taken login or email links a later identity to the account an earlier one created, also through other identities. One of these identities can be active while another is inactive.

The user provenance fields do not record who locked an account.

## Decision

The `users` component locks OpenProject users whose Jira accounts are inactive, and never unlocks a user ([PR #473](https://github.com/netresearch/jira-to-openproject/pull/473)).

- **Inactive means explicit:** only `active: false` on the Jira user proves an account inactive. An identity whose Jira record is missing or has no flag keeps the target unlocked.
- **Creation:** a new user is created locked when every Jira identity that can end up on the same account is inactive. Identities that share a login or a non-empty email, directly or through others, form one group. If any identity in the group is active, the account is created active.
- **Re-runs:** after creating users, the component locks every mapped OpenProject user whose mapped Jira identities are all inactive. One active identity keeps the account unlocked. Users that are already locked are skipped.
- **Administrators:** OpenProject administrators are never locked. They are skipped with a warning.
- **Confirmation:** the Ruby statement locks each user in a transaction and reads the committed row back. A lock that OpenProject does not confirm raises a `MigrationError`.
- **No unlock:** because provenance cannot tell an administrator's lock from the tool's own, the tool only locks.

## Consequences

### Positive

- A Jira account that can no longer log in does not become a usable OpenProject login.
- Group membership no longer gives inactive users read access to migrated work packages. Three exceptions keep access: an inactive identity that shares an OpenProject account with an active identity or with one whose Jira status is unknown (no `active` flag), and OpenProject administrators, which are never locked.
- A shared OpenProject account is created and kept usable as long as one of its Jira identities is active. An account that is already locked stays locked even if an identity becomes active again; an administrator has to unlock it.
- Locks set by administrators survive re-runs.

### Negative

- A user who becomes active in Jira again after a run stays locked in OpenProject. An administrator has to unlock the account.
- OpenProject accounts that existed before the migration and are matched to an inactive Jira account are locked too.
- OpenProject administrators whose Jira account is inactive stay unlocked; the run only logs a warning.
- Locked users are no longer valid values of user custom fields, because OpenProject checks project principals. Such a value becomes a counted refusal.
- The Ruby lock statement has not run against a real OpenProject yet.
