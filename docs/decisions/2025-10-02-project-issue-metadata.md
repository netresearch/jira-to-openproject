# ADR: Project & Issue Metadata Alignment

**Status:** Accepted

## Context

Recent migrations confirm user provenance and locale handling, but project and issue metadata still show noticeable gaps. Discovering Jira payloads and current migrations revealed:

- Jira projects expose lead, description, type, category, and Tempo account links; only name/identifier/description and Tempo parent are migrated today.
- OpenProject projects default to modules `work_package_tracking`, `wiki`, `costs`; additional module enablement is static and ignores Jira usage.
- Jira issues expose multiple custom start-date fields (e.g. `customfield_12590`, `customfield_18690`, `customfield_11490`), but the work-package pipeline only populates `due_date` and writes the raw custom fields as text.
- Work-package migrations rely on custom modules (components, labels, resolutions, versions, estimates, etc.) that are not part of the default component list, so metadata is skipped unless operators add components manually.
- QA tooling (`scripts/data_qa.py`) does not report metadata coverage, making rehearsal validation manual.

## Decision

1. **Project metadata scope**
   - Map Jira project lead to OpenProject project membership (member + role) and record it in a provenance custom field for traceability.
   - Enable additional project modules (`time_tracking`, `news`, `calendar`, `cost_control`) when the Jira project uses Tempo accounts or has related data (configurable).
   - Persist Jira project avatar URL and optional category/custom attributes in dedicated project custom fields for operator reference.

2. **Issue metadata scope**
   - Convert Jira start-date fields into OpenProject `start_date`, using precedence rules (`Target start` > `Change start date` > `Start`) and fall back to the first transition into an *In Progress* status category when custom fields are absent. Keep original custom fields as backup values.
   - Ensure built-in metadata migrations (priorities, versions, components, resolutions, labels, estimates, security levels, votes, remote links, attachments, relations) run in the default pipeline so issue metadata is complete without extra CLI switches.
   - Leave avatar migration optional; log when Jira returns only gravatar URLs without custom images (current environment).

3. **QA & documentation**
   - Extend `scripts/data_qa.py` to report project lead/module coverage and issue start-date population for sampled projects.
   - Update README/AGENTS to describe the enriched project/issue metadata behavior and the expanded default component order.

## Consequences

- Project migrations will perform additional Rails operations (module toggles, membership updates); we need to guard these with idempotent scripts and re-use the head/body Ruby pattern.
- Issue migrations will now depend on the timestamp migrator for start-date writes; this will require additional tests for locale/date parsing.
- Operators get richer QA output and can rely on the default component list for full metadata coverage.

## Follow-up Tasks

1. Persist additional Jira project metadata (categories, types, optional avatars) alongside the lead provenance in `project_migration.py`.
2. Implement heuristics that enable OpenProject modules (time tracking, news, calendar, cost control, etc.) based on Jira/Tempo usage.
3. Resolve priority mapping preflight warnings by seeding mappings or arranging components so `priorities` runs before `work_packages`.
4. Expand default component list in `migration.py` and verify sequence/failure handling.
5. Enhance QA script and README/AGENTS with new guidance.
