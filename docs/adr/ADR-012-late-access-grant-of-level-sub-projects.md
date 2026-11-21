# ADR-012: Late access grant of the level sub-projects

## Status

Accepted (2026-11-21)

## Context

[ADR-009](ADR-009-security-levels-as-private-sub-projects.md) puts the issues of a Jira security level into a private OpenProject sub-project, one per Jira project and level, and leaves it without members until the late access grant (order of work, step 6). Its members are to be exactly the level's static readers, and on Enterprise each `per_issue` work package is to be shared with exactly its extra readers. The importer read only the shared report, which carries counts; the reader names are in the operator archive.

Measured on the pilot (OpenProject 17.9.7, Community, rails runner in a rolled-back transaction, 2026-11-21):

- Roles: "Member" (3), "Reader" (4), "Project admin" (5) are non-builtin project roles; "Work package viewer" (8) is the `WorkPackageRole` with builtin value 3 (`Role::BUILTIN_WORK_PACKAGE_VIEWER`).
- `Members::CreateService` refuses a locked user: "User or group cannot be assigned to a project." (`app/contracts/members/create_contract.rb`, `principal_assignable`). `Shares::CreateContract` refuses locked users the same way.
- `Shares::CreateService` always sends the "work package shared" notification with `send_notifications: true` (`app/services/shares/create_service.rb:62`), which enqueues `Mails::WorkPackageSharedJob` (`config/initializers/subscribe_listeners.rb:101-106`).
- A user who is a member of the parent only through a group holds the group's roles as inherited member roles, and the grant's read returns them (a group with "Member" in the parent, two users added with `Groups::AddUsersService`).
- After the grant, a reader sees both work packages of the sub-project, a member of the parent only sees none, and a user with a share sees only the shared work package; `WorkPackage.visible` does not check the Enterprise token.

Parent access comes from two places: `admin_schemes` grants direct user memberships through `Members::CreateService` (so never to a locked user), and `groups` grants the `J2O Role <KEY>::<role>` groups, whose roles OpenProject copies to the group's users ([ADR-005](ADR-005-project-access-after-memberless-staging.md)).

## Decision

1. **Input.** `j2o-restricted inventory` writes an operator-only readers file (`readers.json`, mode 600) next to its report: per level the static readers, per `per_issue` issue its extra readers, as Jira user names, with the report's `archive_sha256`. Groups and project roles are already expanded into users and intersected with Browse, so the file names users only, and the grant creates user memberships only, never a group membership (ADR-009, decision 1). The importer reads it from `migration.security_level_readers` and refuses a file other users can read or one whose levels, reader counts or `per_issue` keys differ from the report.
2. **Users.** A reader name maps to the OpenProject user whose user-mapping entry has that `jira_name` (case-insensitive), as in `j2o-restricted admins`. A name with no such entry, with entries for two users, or only with an entry restored from OpenProject (`restored_from_op`) gets nothing and is counted (`readers_without_user`); it is never guessed.
3. **Role.** A static reader gets the project roles it holds in the parent project's OpenProject project, including roles inherited from the `J2O Role` groups, read live when the grant runs. A reader without a project role in the parent gets "Reader", the seeded view-only role. A reader therefore never gets more in the sub-project than in the parent, except "Reader" for a reader the parent does not know, whom Jira lets read the level's issues.
4. **Shares.** On Enterprise (the edition of the administrator report), each `per_issue` work package is shared with the users of its issue's extra readers, with "Work package viewer", found by its builtin value. The share is saved as a `Member` row with the work package as entity, not through `Shares::CreateService`, so that no "shared with you" mail goes out during the migration; that service adds nothing else for a user principal. Community gets no shares; `per_issue` issues are not migrated there (ADR-009, decision 2).
5. **Locked users.** A locked reader gets no new membership and no new share, as `admin_schemes` gives no direct membership to a locked user in the parent; the count is `locked_readers`. On the pilot none of the 52 locked users held any project membership (2026-11-21). A row of a reader who was locked after the grant (`users` locks newly inactive Jira users on every run, ADR-006) is kept and accepted when it has the expected roles. A locked user cannot log in, so the grant does not narrow what a reader can do today; a repeat run after an administrator unlocks the user grants the membership.
6. **Read back.** Every membership row of the sub-project is read before and after writing. The rows read back must equal the expected rows exactly: user, shared work package (or none) and role set. Anything else fails the sub-project.
7. **Foreign rows.** A row the grant would not have written (another user, a group, another role set, a share of another work package or with another user) refuses the sub-project before any write. The grant deletes and changes nothing: without provenance on memberships it cannot tell its own rows from an administrator's. A reader whose parent roles changed after the grant is therefore reported, not updated.
8. **Order.** `security_level_access` runs after `admin_schemes` and before `reporting`, after every component that writes level work packages and after the parent's memberships exist.

## Consequences

### Positive

- A level sub-project's members are exactly its static readers with an OpenProject user, read back after writing; on Enterprise a `per_issue` work package is visible to exactly its extra readers in addition.
- A grant to a user without Browse never reaches OpenProject: the readers are the grants intersected with Browse.
- The readers file never enters the shared output; refusals name levels and issue keys only.
- A repeat run writes nothing.

### Negative

- A stale membership (a reader removed in Jira after the grant) and a changed parent role block the sub-project until an administrator removes or corrects the row; the grant only reports it.
- A reader whose Jira name is not in the user mapping, or maps to two OpenProject users, gets no access; the count is reported.
- Locked readers get no membership until they are unlocked and the grant runs again.
- Shares are saved without `Shares::CreateService`; a later OpenProject version that adds behaviour to that service is not covered by this path.
- The grant reads each reader's roles in the parent; a reader who is a member of the parent through a group shows the group's roles as inherited roles, which are copied as direct roles.
