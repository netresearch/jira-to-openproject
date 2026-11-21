# ADR-004: Audience-safe Jira field history as a structured value

## Status

Accepted (2026-11-20)

Extends the [2026-11-19 update of ADR-001](ADR-001-two-phase-work-package-migration.md#update-2026-11-19-field-change-history).

## Context

Jira keeps a field-change history (changelog) per issue. OpenProject shows history as journals on a work package. Replaying the changelog as native journals was the obvious way to carry it over.

Native replay could not keep each change's reader audience. Since [PR #387](https://github.com/netresearch/jira-to-openproject/pull/387) the legacy `work_packages` writer rejects every record with pending journal operations (`LegacyJournalReplayError`) before it writes anything, and points operators to `work_packages_skeleton`, `work_packages_content` and the protected-comment archive.

Not every changelog entry has the issue's audience:

- In Jira Server/DC the History tab is readable by everyone who can browse the issue.
- A worklog can carry its own visibility, and a changelog event that touches a worklog (`WorklogId`, `WorklogTimeSpent`, and the time-tracking totals it changes) exposes it.
- Comment changes and events or items with audience or visibility metadata belong to a narrower circle than the issue.
- Issues with a security level are not migrated as ordinary work packages.

## Decision

The `work_package_field_history` component ([PR #461](https://github.com/netresearch/jira-to-openproject/pull/461)) stores each migrated issue's audience-safe field changes as one structured value per work package. It does not create native journals.

- **Storage:** canonical JSON (sorted keys, no whitespace, UTF-8) in the work-package text custom field "Jira Field Changes (structured history)". The value has the shape `{"jiraIssueId", "events": [{"id", "created", "author", "changes": [...]}]}`. An issue with no kept change gets no value.
- **Selection:** `src/infrastructure/jira/field_history.py`, shared with the operator archive `j2o-history`. It keeps recognized built-in fields, supported Agile and Tempo fields, and reviewed custom fields listed in `migration.history_approved_field_ids` (empty by default). It withholds comment changes and every event or item with audience or visibility metadata. For the per-work-package value (`project_visible=True`) it also drops every event that touches a worklog; the operator archive keeps those events.
- **Scope:** only issues that Jira returns for an empty security level in software and business projects. Other project types are skipped. An issue whose changelog is not one complete page is skipped and counted as incomplete.
- **Write:** the component checks each target's `J2O Origin ID` and project, enables the field for the project and type, and stores the custom value directly, as `customfields_generic` does, without saving the work package. A value is written only when it differs from the stored one.
- **Re-runs clear stale values:** when an issue gains a security level or keeps no change, a re-run removes the stored value and its copies in the work package's journals (`Journal::CustomizableJournal`). A work package that holds a value but no mapping entry is found through its `J2O Origin ID` and `J2O Origin Key`. A removal that cannot be confirmed counts as `clear_unconfirmed` and makes the result unsuccessful.
- **Position in the run:** directly after `customfields_generic` in the default and `full` profiles.

On 2026-11-19 the project owner decided that native OpenProject activity entries are to follow for changes that every reader of the work package could see. This ADR does not implement them.

### Update 2026-11-21: level work packages (ADR-009)

With `migration.security_level_work_packages` on, the scope also covers each selected issue with a security level: its value goes into the work package its level sub-project holds for it ([ADR-009](ADR-009-security-levels-as-private-sub-projects.md)). The readers of that work package are the sub-project's members, who are the level's static readers, and on Enterprise the issue's extra readers once the late access grant shares it with them. All of them can browse the issue in Jira, and the History tab there shows every changelog entry to everyone who can browse the issue, so the same selection applies. A change of the security level itself is kept, also one that names another level: Jira shows it to the same readers. This rests on the History-tab premise above; it has not been measured against a level issue. Worklog events, comment changes and audience metadata stay withheld, and an ordinary work package of an issue that now has a level still gets its value removed.

## Consequences

### Positive

- Field history reaches OpenProject without widening its audience: the kept changes have the same readers as the work package.
- The operator archive and the standard run use one selection, so they cannot drift apart.
- Repeat runs write nothing when nothing changed, and they remove values that have become unsafe. An issue the run skips (for example an incomplete changelog page or an unsupported project type) keeps its earlier value; the run reports it in its skip counters, and an operator reviews or removes such values before granting access.
- In the live run reported in PR #461 (one project, 94 issues), 2827 of 2866 changes were kept, and the stored values were byte-equal to the computed rows.

### Negative

- History is not shown as native activity entries. Users read a JSON value, not the activity stream, until the follow-up decided on 2026-11-19 lands.
- Comment changes and worklog-related changes are absent from the stored value. Of these, only the worklog-related changes are kept in the operator archive.
- Free-text standard custom fields stay out until someone reviews them and lists them in `migration.history_approved_field_ids`.
- The removal path of the Ruby write script and the provenance lookup for unmapped work packages have not run against a real OpenProject yet.
- A value whose work package has no `J2O Origin ID` cannot be confirmed (`clear_unconfirmed`) and has to be removed by hand.
- Whether OpenProject shows the stored value in the journal of the next user edit has not been measured.
