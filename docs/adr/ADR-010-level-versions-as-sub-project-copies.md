# ADR-010: Versions of level work packages are copies in their sub-project

## Status

Accepted (2026-11-21)

## Context

[ADR-009](ADR-009-security-levels-as-private-sub-projects.md) puts each issue with a Jira security level into a private, memberless OpenProject sub-project of its project's OpenProject project and left open how such a work package gets its versions: the fix versions (`versions`), the affected versions (`affects_versions`) and the sprint version (`sprint_epic`). The versions belong to the parent project, which every reader of the parent project sees. Two rolled-back probes on OpenProject 17.9.7 (the comments [7065082968](https://github.com/netresearch/jira-to-openproject/pull/486#issuecomment-7065082968) and [7065329324](https://github.com/netresearch/jira-to-openproject/pull/486#issuecomment-7065329324) on pull request #486) and the source of that version (read in the pilot container under `/app`) show:

- A work package may only use a version shared with its project. `validate_target_versions_are_assignable` and `validate_observed_in_versions_are_assignable` reject any other ID (`app/contracts/work_packages/base_contract.rb:421-444`). `update_versions` prunes the links of a work package to a version that is not shared with its project, through `project.shared_versions` (`app/models/work_package/versions.rb:98-135`). A parent version with sharing `none` is therefore not assignable to a work package of the sub-project; with `descendants` it is.
- A version counts every targeted work package without a visibility check: `issues_count`, `open_issues_count`, `closed_issues_count`, `estimated_hours` and `spent_hours` query `targeted_work_packages` directly (`app/models/version.rb:100-169`). The version overview partial shows these counts and the completion bar (`app/views/versions/_overview.html.erb:56-76`), and the roadmap renders it for every version (`app/views/versions/index.html.erb:60`). In the probe, a parent-only member who could not see the sub-project's work package saw "2 open" on the shared version.
- Native sprint sharing in the Backlogs module is one project setting for all sprints of a project (`no_sharing`, `share_all_projects`, `share_subprojects`, `receive_shared`; `modules/backlogs/app/models/projects/sprint_settings.rb:34-39`), not a setting per version.
- Observed-in links (affected versions) are not counted by `issues_count`, but they are subject to the same assignability rule, so a work package of the sub-project cannot link a parent version either.
- A version owned by the private sub-project, with sharing `none`, is assignable to its work packages, may carry the same name as a version of the parent, counts only its own work packages, and is invisible to a member of the parent only (`Version.visible` false, the parent's roadmap with sub-projects false).

## Decision

Each level sub-project gets its own copy of every parent version that a selected level work package uses, as a fix version, an affected version or a sprint version.

- **The copy.** It belongs to the sub-project, has sharing `none` and the source version's name, and the description, status, start date and release date that the ordinary path sets from the Jira release catalogue (for fix and affected versions). Only versions a selected level work package uses are copied, not the whole catalogue.
- **Provenance and repeat runs.** The copy carries the same `Jira Version Origin` value as a parent version (Jira base URL, version ID, released and archived flags). `Jira Version Origin` is read per project (`project.versions`) by both the catalogue writer and the observed-version writer, so a copy and its parent version never meet in one lookup. A repeat run adopts the copy by its origin instead of creating a second one. A version of the sub-project with the same name and no origin stops that sub-project's transaction.
- **Mapping.** The copies are recorded in `security_level_version_mapping.json`, keyed by `<sub-project label>:<Jira version ID>` (for example `APP:10000:41`), never in `version_mapping.json`. Sprint copies are planned under the key `<sub-project label>:sprint:<Jira sprint ID>`, because sprint IDs and version IDs are separate ID spaces.
- **Memberless staging.** A copy is created or changed only while the sub-project has no membership row at all, locked users and work package shares included ([ADR-005](ADR-005-project-access-after-memberless-staging.md)), as the categories of the components stage do. An unchanged copy is still adopted after the access grant. The assignment of a copy to a work package is not gated: it changes only work packages the sub-project's members can already read.
- **The parent.** The parent's versions and their sharing are never changed. The sub-project transaction checks the Jira origin of the parent and that the sub-project is its private child.
- **Selection.** Each component resolves the level work packages through the shared resolver of ADR-009 before any write, reads each Jira project once with the level search, and refuses an issue whose level changed or that the search omits. Unselected and archived issues get nothing.

### Order of work

1. Fix versions and affected versions (`versions`, `affects_versions`).
2. Sprint versions (`sprint_epic`), after pull request #486 (boards) is merged; until then a level work package gets no sprint. Native completed sprints (`src/ruby/import_completed_sprints.rb`) stay out of scope: level issues stay out of their manifest.

## Alternatives considered

- **Share the parent version with `descendants`.** Rejected: the counts, hours and completion of the shared version include the level work packages and are shown to every reader of the parent project.
- **No versions on level work packages.** Rejected: the readers of a level see the issue's fix versions, affected versions and sprint in Jira; leaving them out loses data they can see.

## Consequences

### Positive

- A level work package keeps its fix versions, affected versions and sprint, and the version counts of the parent show nothing of it.
- A repeat run, also one after a lost mapping file, adopts the existing copies.

### Negative

- A version or sprint is split across the parent and each sub-project that uses it: the parent's version, its roadmap entry and its sprint board do not show level work packages, and each copy counts only the work packages of its own sub-project.
- A change of a Jira version (renamed, released, new dates) reaches a copy only while its sub-project is memberless; after the access grant a changed copy stops that sub-project's transaction and its issues are refused.
- Versions created in OpenProject later are not copied: only versions a selected level work package uses in Jira exist in the sub-project.
