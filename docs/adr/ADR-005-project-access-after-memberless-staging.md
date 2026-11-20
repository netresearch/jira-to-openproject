# ADR-005: Grant project access after memberless staging

## Status

Accepted (2026-11-20)

## Context

Before [PR #462](https://github.com/netresearch/jira-to-openproject/pull/462), a first `j2o migrate --profile full` run gave no test account access to any migrated work package. Four defects caused this:

- Change detection skipped components whose entities have no ID, among them `admin_schemes`.
- `groups` ran before `projects`, but it reads the project mapping to build project-role groups.
- Membership writes saved `Member` records directly. That bypassed OpenProject's member services, so members of a group did not inherit the group's roles.
- Users created during the run lost their OpenProject ID because a cached user list was reused.

Some stages may only write while a project has no members. The Ruby of `simpletasks` (`tasklist_origins.py`) raises "Stage checklist changes before granting project access" when the project has members. The Ruby of `components` (`component_assignments.py`) refuses to rename, create or assign categories once the project has members. Other stages need members: `category_defaults` confirms the default assignee's existing membership, and `admin_schemes` grants role memberships itself.

PR #462 moved `groups` directly after `projects`. From then on, `simpletasks` refused on every first run.

## Decision

Project access is granted only after the stages that need memberless projects ([PR #462](https://github.com/netresearch/jira-to-openproject/pull/462), [PR #468](https://github.com/netresearch/jira-to-openproject/pull/468)).

- **Order:** `groups` runs in the finalization block of `DEFAULT_COMPONENT_SEQUENCE`, after `time_entries`. The block is `groups`, `category_defaults`, `admin_schemes`, `reporting`. So `groups` runs after `projects`, after the memberless stages `simpletasks` and `components`, and before `category_defaults` and `admin_schemes`.
- **Guard:** `tests/unit/test_component_order.py` asserts this order for the default sequence and the `full` profile. Every stage in `MEMBERLESS_STAGING` must run before `groups`, and every stage in `NEEDS_MEMBERS` after it.
- **Membership writes:** memberships go through OpenProject's member services instead of `Member#save`. New memberships use `Members::CreateService`, and changed role sets use `Members::UpdateService`. Group memberships additionally call `Groups::CreateInheritedRolesService` for the group's users and the project. This also repairs group memberships that an earlier run saved directly, and it inserts only missing rows. Role changes compare direct roles only, so roles inherited from groups stay untouched.
- **Supporting fixes in PR #462:** change detection runs components whose entities lack an ID, and the user list is re-read from OpenProject after users are created.

## Consequences

### Positive

- A single first run, without manual steps, gives authorized accounts access to their work packages. In the local acceptance reported in PR #462, this held for ordinary, group and project-role cases, and unauthorized accounts saw no restricted comment.
- Members of a group inherit the group's project roles.
- Stages that must write into memberless projects can do so on a first run. Projects stay without members until that staging is done.
- A change of the order that breaks either constraint fails a unit test.

### Negative

- On a first run, migrated users get project access only when the run reaches `groups` in the finalization block.
- `admin_schemes` expects a role named "Project member", which OpenProject 17.9.7 does not have. Access currently comes through the project-role groups.
- A repeat run does not re-run `groups` when only project roles changed.
- That the stages between `labels` and `time_entries` work without members is read from the code, not measured. The next live run settles it.
