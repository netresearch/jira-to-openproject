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
3. **Role.** Update 2026-11-21 (review by the coordinator): a parent role that lets its holder widen the audience is not carried over: any role granting `manage_members`, `invite_members_by_email`, `share_work_packages`, `share_sprint` or `edit_project` (which can make the project public; `public` is a writable attribute of `Projects::BaseContract`, app/contracts/projects/base_contract.rb:39) is dropped, and a reader left without a role gets "Reader"; "Reader" itself is refused if it grants one of these. On the pilot, "Project admin" (5) grants all five, "Member" (3) and "Reader" (4) none; in a rolled-back probe a parent "Project admin" got "Reader" and a parent "Member" kept "Member". Otherwise: A static reader gets the project roles it holds in the parent project's OpenProject project, including roles inherited from the `J2O Role` groups, read live when the grant runs. A reader without a project role in the parent gets "Reader", the seeded view-only role. A reader therefore never gets more in the sub-project than in the parent, except "Reader" for a reader the parent does not know, whom Jira lets read the level's issues.
4. **Shares.** On Enterprise (the edition of the administrator report), each `per_issue` work package is shared with the users of its issue's extra readers, with "Work package viewer", found by its builtin value. The share is saved as a `Member` row with the work package as entity, not through `Shares::CreateService`, so that no "shared with you" mail goes out during the migration; that service adds nothing else for a user principal. Community gets no shares; `per_issue` issues are not migrated there (ADR-009, decision 2).
5. **Locked users.** A locked reader gets no new membership and no new share, as `admin_schemes` gives no direct membership to a locked user in the parent; the count is `locked_readers`. On the pilot none of the 52 locked users held any project membership (2026-11-21). A row of a reader who was locked after the grant (`users` locks newly inactive Jira users on every run, ADR-006) is kept and accepted when it has the expected roles. A locked user cannot log in, so the grant does not narrow what a reader can do today; a repeat run after an administrator unlocks the user grants the membership.
6. **Read back.** Every membership row of the sub-project is read before and after writing. The rows read back must equal the expected rows exactly: user, shared work package (or none) and role set. Anything else fails the sub-project.
7. **Foreign rows.** A row the grant would not have written (another user, a group, another role set, a share of another work package or with another user) refuses the sub-project before any write. The grant deletes and changes nothing: without provenance on memberships it cannot tell its own rows from an administrator's. A reader whose parent roles changed after the grant is therefore reported, not updated.
8. **Order.** `security_level_access` runs after `admin_schemes` and before `reporting`, after every component that writes level work packages and after the parent's memberships exist.
9. **Stages after the grant.** The memberless checks of the level stages (the skeleton's re-read in `verify_level_project`, the repeat check of `security_level_projects`, `components`, `simpletasks` and the version copies of ADR-010) accept a sub-project whose membership rows are all grants of its readers, so that repeat runs keep working: a user's project membership whose user is a static reader with an OpenProject user, or a share of one of the sub-project's work packages, identified by its `J2O Origin Key`, with a user of that issue's extra readers. Any other row refuses. The check is one Ruby lambda (`src/infrastructure/openproject/level_audience.py`) fed with the audience computed from the readers file; without a readers file the audience is empty and the check is the memberless one. Per writer, a change after the grant is still safe: a new work package, category, checklist or version copy of a sub-project is visible to its members, who are all static readers and read every issue of the level in Jira; a share-only reader sees only the shared work package, which gets no fix or sprint version (ADR-010) and whose category and checklist are its own issue's. The check does not compare roles; the grant does.
10. **Watchers and assignee.** With the readers file, `watchers` adds to a level work package only the watchers whose OpenProject user is a static reader of its level, or on Enterprise an extra reader of its issue, and counts the others (`watchers_not_readers`); the skeleton assigns a level work package only to such a reader and leaves another assignee out (`assignees_not_readers`) instead of making it a member. Without a readers file the level work packages get no watchers and the assignee is set as before.
11. **Switch.** `migration.security_level_work_packages` is `true` in the shipped configuration, and `security_level_projects` and `security_level_access` are in the default and `full` profiles. The level path runs only once `security_level_report` is set; without an inventory, issues with a level stay out of the migration as with the switch off, because the ordinary path never selects them. A configured report that is missing or does not match still stops each level component before any write.

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
