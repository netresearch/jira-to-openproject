# Jira to OpenProject Entity Mapping Reference

**Version**: 2.0
**Last Updated**: 2025-12-11

This document provides a comprehensive mapping of how Jira entities are transformed into OpenProject entities during migration.

---

## Quick Reference: Entity Mapping Summary

| Jira Entity | OpenProject Entity | Migration Component | Notes |
|-------------|-------------------|---------------------|-------|
| **Customer** (Tempo) | Top-level Project | `companies` | Hierarchy root |
| **Account** (Tempo) | Custom Field on Project | `accounts` | Financial tracking |
| **Project** | Sub-Project | `projects` | Under customer hierarchy |
| **Issue** | Work Package | `work_packages` | Core entity |
| **User** | Principal (User) | `users` | With provenance |
| **Group** | Group | `groups` | Role-based |
| **Issue Type** | Work Package Type | `issue_types` | Type system |
| **Status** | Status | `status_types` | Workflow states |
| **Priority** | Priority | `priorities` | Ordering preserved |
| **Resolution** | Custom Field Value | `resolutions` | On "Resolution" CF |
| **Component** | Custom Field Value | `components` | On "Component" CF |
| **Version** | Version | `versions` | Release tracking |
| **Sprint** | Version (Sprint type) | `sprint_epic` | Agile planning |
| **Epic** | Work Package (Epic type) | `sprint_epic` | Hierarchy parent |
| **Label** | Tag | `labels` / `native_tags` | Categorization |
| **Attachment** | Attachment | `attachments` | File transfer |
| **Comment** | Journal Entry | `work_packages` | Activity history |
| **Worklog** | Time Entry | `time_entries` | Time tracking |
| **Issue Link** | Relation | `relations` | Work package links |
| **Link Type** | Relation Type | `link_types` | Relation taxonomy |
| **Watcher** | Watcher | `watchers` | Notification |
| **Vote** | Custom Field Value | `votes_reactions` | On "Votes" CF |
| **Board** | Saved Query | `agile_boards` | Kanban/Scrum views |
| **Filter** | Saved Query | `reporting` | Saved searches |
| **Dashboard** | Wiki Page | `reporting` | Project overview |
| **Role Membership** | Project Membership | `admin_schemes` | Access control |

---

## Detailed Entity Mappings

### 1. Organizational Hierarchy

```
JIRA STRUCTURE                    OPENPROJECT STRUCTURE
──────────────────────────────    ──────────────────────────────
Tempo Customer (ACME Corp)   ──→  Top-Level Project (acme-corp)
    │                                  │
    ├── Tempo Account (Contract)       ├── Custom Field: Tempo Account
    │                                  │
    └── Jira Project (PROJ)       ──→  └── Sub-Project (proj)
            │                                  │
            └── Issue (PROJ-123)     ──→       └── Work Package (WP#456)
```

#### Tempo Customer → OpenProject Top-Level Project

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `customer.name` | `project.name` | Direct mapping |
| `customer.key` | `project.identifier` | Slugified |
| `customer.id` | Custom Field: "Tempo Customer ID" | Provenance |
| - | `project.parent_id` | null (top-level) |

**Component**: `companies` (`CompanyMigration`)

#### Tempo Account → OpenProject Custom Field

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `account.name` | Custom Field Value | On project |
| `account.key` | Custom Field: "Tempo Account Key" | Lookup key |
| `account.category` | Custom Field: "Tempo Account Category" | Classification |
| `account.lead` | Project membership | Optional lead role |

**Component**: `accounts` (`AccountMigration`)

---

### 2. Project Migration

```
Jira Project                      OpenProject Sub-Project
──────────────────────────────    ──────────────────────────────
key: "PROJ"                  ──→  identifier: "proj"
name: "My Project"           ──→  name: "My Project"
lead: "john.doe"             ──→  membership(role: Project admin)
category: "Development"      ──→  CF: "Jira Project Category"
projectType: "software"      ──→  CF: "Jira Project Type"
                                  CF: "Jira Project URL"
                                  CF: "Jira Project Avatar URL"
                                  CF: "Jira Project Key"
                                  CF: "Jira Project ID"
```

#### Jira Project → OpenProject Project

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `project.key` | `project.identifier` | Lowercased |
| `project.name` | `project.name` | Direct mapping |
| `project.description` | `project.description` | HTML converted |
| `project.lead.accountId` | Membership (Project admin) | Role assignment |
| `project.projectCategory.name` | CF: "Jira Project Category" | Provenance |
| `project.projectTypeKey` | CF: "Jira Project Type" | software/business |
| - | CF: "Jira Project URL" | Link to original |
| `project.avatarUrls` | CF: "Jira Project Avatar URL" | Image link |

**Modules Enabled**:
- Always: `work_package_tracking`, `wiki`
- Tempo-linked: `time_tracking`, `costs`
- Has categories: `calendar`, `news`

**Component**: `projects` (`ProjectMigration`)

---

### 3. Work Package Migration (Two-Phase)

The work package migration uses a **two-phase approach** to correctly resolve cross-references:

```
PHASE 1: work_packages_skeleton        PHASE 2: work_packages_content
─────────────────────────────────      ─────────────────────────────────
Creates minimal WP:                    Populates full content:
  - type, status, subject              - description (links resolved)
  - project assignment                 - custom field values
  - J2O Origin Key                     - journals/comments
                                       - PROJ-123 → WP#456 conversion
         │
         └──→ work_package_mapping.json ──→ Used for link resolution
```

#### Jira Issue → OpenProject Work Package

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `issue.key` | CF: "J2O Origin Key" | e.g., "PROJ-123" |
| `issue.id` | CF: "Jira Issue ID" | Numeric ID |
| `issue.fields.summary` | `work_package.subject` | Title |
| `issue.fields.description` | `work_package.description` | ADF→Markdown, links converted |
| `issue.fields.issuetype.name` | `work_package.type` | Mapped via issue_types |
| `issue.fields.status.name` | `work_package.status` | Mapped via status_types |
| `issue.fields.priority.name` | `work_package.priority` | Mapped via priorities |
| `issue.fields.assignee` | `work_package.assignee` | User lookup |
| `issue.fields.reporter` | `work_package.author` | User lookup |
| `issue.fields.created` | `work_package.created_at` | Timestamp preserved |
| `issue.fields.updated` | `work_package.updated_at` | Timestamp preserved |
| `issue.fields.duedate` | `work_package.due_date` | Date mapping |
| `issue.fields.customfield_*` | `work_package.start_date` | Start date derivation* |
| `issue.fields.project.key` | `work_package.project` | Project lookup |
| `issue.fields.parent` | `work_package.parent` | Hierarchy preserved |

**Start Date Derivation** (precedence order):
1. `customfield_18690` (Start Date)
2. `customfield_12590` (Planned Start)
3. `customfield_11490` (Target Start)
4. `customfield_15082` (Sprint Start)
5. First "In Progress" status transition timestamp

**Components**:
- Phase 1: `work_packages_skeleton` (`WorkPackageSkeletonMigration`)
- Phase 2: `work_packages_content` (`WorkPackageContentMigration`)
- Legacy: `work_packages` (`WorkPackageMigration`)

---

### 4. User Migration

```
Jira User                         OpenProject Principal
──────────────────────────────    ──────────────────────────────
accountId: "abc123"          ──→  CF: "J2O Origin User ID"
displayName: "John Doe"      ──→  firstname + lastname
emailAddress: "j@e.com"      ──→  mail
locale: "en_US"              ──→  language: "en"
timeZone: "America/NY"       ──→  CF: "User Timezone"
avatarUrls                   ──→  Avatar (via Avatars module)
```

#### Jira User → OpenProject User

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `user.accountId` | CF: "J2O Origin User ID" | Primary lookup |
| `user.key` (Server) | CF: "J2O Origin User Key" | Legacy Jira Server |
| `user.displayName` | `firstname` + `lastname` | Parsed |
| `user.emailAddress` | `mail` | Unique constraint |
| `user.locale` | `language` | Locale→Language mapping |
| `user.timeZone` | CF: "User Timezone" | For timestamp conversion |
| `user.avatarUrls.48x48` | Avatar attachment | Via Avatars module |
| - | CF: "J2O Origin System" | "jira" |
| - | CF: "J2O Origin URL" | Link to Jira profile |

**Component**: `users` (`UserMigration`)

---

### 5. Issue Type Migration

```
Jira Issue Type                   OpenProject Work Package Type
──────────────────────────────    ──────────────────────────────
name: "Bug"                  ──→  name: "Bug"
description: "..."           ──→  description: "..."
subtask: false               ──→  is_milestone: false
iconUrl: "..."               ──→  color (derived)
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `issuetype.name` | `type.name` | Direct mapping |
| `issuetype.description` | `type.description` | Optional |
| `issuetype.subtask` | `type.is_milestone` | Subtask→child handling |
| `issuetype.id` | CF: "Jira Issue Type ID" | Provenance |

**Component**: `issue_types` (`IssueTypeMigration`)

---

### 6. Status Migration

```
Jira Status                       OpenProject Status
──────────────────────────────    ──────────────────────────────
name: "In Progress"          ──→  name: "In Progress"
statusCategory: "indeterminate" → is_closed: false
                                  position: (preserved)
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `status.name` | `status.name` | Direct mapping |
| `status.statusCategory.key` | `status.is_closed` | "done"→true |
| `status.id` | CF: "Jira Status ID" | Provenance |
| - | `status.position` | Order preserved |

**Status Category Mapping**:
- `new` → Open (is_closed: false)
- `indeterminate` → In Progress (is_closed: false)
- `done` → Closed (is_closed: true)

**Component**: `status_types` (`StatusMigration`)

---

### 7. Comment/Journal Migration

```
Jira Comment                      OpenProject Journal
──────────────────────────────    ──────────────────────────────
body: "See PROJ-123"         ──→  notes: "See WP#456" (converted)
author: {...}                ──→  user_id (preserved via Rails)
created: "2024-..."          ──→  created_at (preserved via Rails)
updated: "2024-..."          ──→  updated_at
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `comment.body` | `journal.notes` | ADF→Markdown, links converted |
| `comment.author.accountId` | `journal.user_id` | Author preserved |
| `comment.created` | `journal.created_at` | Timestamp preserved via Rails |
| `comment.updated` | `journal.updated_at` | If edited |

**Link Conversion in Comments**:
- `PROJ-123` → `WP#456` (using work_package_mapping.json)
- `@accountId` → `@username` (user mention)
- `[text\|url]` → `[text](url)` (Wiki markup)

**Component**: Part of `work_packages_content` (`WorkPackageContentMigration`)

---

### 8. Attachment Migration

```
Jira Attachment                   OpenProject Attachment
──────────────────────────────    ──────────────────────────────
filename: "doc.pdf"          ──→  file (binary transfer)
author: {...}                ──→  author_id (preserved)
created: "2024-..."          ──→  created_at (preserved)
size: 12345                  ──→  filesize
mimeType: "application/pdf"  ──→  content_type
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `attachment.filename` | `attachment.file.filename` | Direct mapping |
| `attachment.content` | `attachment.file` | Binary download/upload |
| `attachment.author` | `attachment.author_id` | Via Rails metadata |
| `attachment.created` | `attachment.created_at` | Via Rails metadata |
| `attachment.size` | `attachment.filesize` | Byte count |
| `attachment.mimeType` | `attachment.content_type` | MIME type |
| `attachment.id` | CF on attachment | Provenance |

**Component**: `attachments` (`AttachmentsMigration`)

---

### 9. Time Entry Migration

```
Jira Worklog (Tempo)              OpenProject Time Entry
──────────────────────────────    ──────────────────────────────
timeSpentSeconds: 3600       ──→  hours: 1.0
author: {...}                ──→  user_id (preserved)
started: "2024-..."          ──→  spent_on
comment: "..."               ──→  comments
billableSeconds: 3600        ──→  CF: "Billable Hours"
```

| Jira Field | OpenProject Field | Notes |
|------------|------------------|-------|
| `worklog.timeSpentSeconds` | `time_entry.hours` | Seconds→Hours |
| `worklog.author` | `time_entry.user_id` | Via Rails metadata |
| `worklog.started` | `time_entry.spent_on` | Date portion |
| `worklog.comment` | `time_entry.comments` | Description |
| `worklog.issue.key` | `time_entry.work_package_id` | WP lookup |
| Tempo: `billableSeconds` | CF: "Billable Hours" | Optional |

**Component**: `time_entries` (`TimeEntryMigration`)

---

### 10. Relation Migration

```
Jira Issue Link                   OpenProject Relation
──────────────────────────────    ──────────────────────────────
type: "Blocks"               ──→  relation_type: "blocks"
inwardIssue: PROJ-1          ──→  from_id: WP#1
outwardIssue: PROJ-2         ──→  to_id: WP#2
```

| Jira Link Type | OpenProject Relation Type | Notes |
|----------------|--------------------------|-------|
| Blocks | blocks | A blocks B |
| Is blocked by | blocked | Inverse |
| Duplicates | duplicates | A duplicates B |
| Is duplicated by | duplicated | Inverse |
| Relates to | relates | Bidirectional |
| Clones | relates | No direct equivalent |
| Parent of | parent | Hierarchy (via parent_id) |
| Child of | child | Hierarchy (via parent_id) |

**Component**: `relations` (`RelationMigration`)

---

### 11. Agile Migration

```
Jira Sprint                       OpenProject Version
──────────────────────────────    ──────────────────────────────
name: "Sprint 1"             ──→  name: "Sprint 1"
startDate: "2024-01-01"      ──→  start_date
endDate: "2024-01-14"        ──→  effective_date (end)
state: "active"              ──→  status: "open"
goal: "..."                  ──→  description
```

```
Jira Board                        OpenProject Saved Query
──────────────────────────────    ──────────────────────────────
name: "Scrum Board"          ──→  name: "Scrum Board"
type: "scrum"                ──→  filters (sprint-based)
columns: [...]               ──→  columns configuration
```

**Component**: `sprint_epic` (`SprintEpicMigration`), `agile_boards` (`AgileBoardMigration`)

---

## Migration Execution Order

The recommended migration sequence ensures dependencies are satisfied:

```
1. FOUNDATION
   └── users          # No dependencies
   └── groups         # Depends on: users

2. CONFIGURATION
   └── custom_fields  # No dependencies
   └── priorities     # No dependencies
   └── link_types     # No dependencies
   └── issue_types    # No dependencies
   └── status_types   # No dependencies

3. ORGANIZATION
   └── companies      # Tempo customers (if using Tempo)
   └── accounts       # Depends on: companies
   └── projects       # Depends on: users, companies

4. WORKFLOWS
   └── workflows      # Depends on: status_types, issue_types

5. CORE CONTENT (Two-Phase)
   └── work_packages_skeleton  # Phase 1: Creates WPs, builds mapping
   └── work_packages_content   # Phase 2: Populates content with resolved links

   OR (Legacy single-phase):
   └── work_packages  # Combined skeleton + content

6. SUPPLEMENTARY
   └── versions       # Depends on: projects
   └── components     # Depends on: projects
   └── labels         # Depends on: work_packages
   └── sprint_epic    # Depends on: projects, work_packages
   └── agile_boards   # Depends on: sprint_epic

7. RELATIONSHIPS
   └── relations      # Depends on: work_packages
   └── watchers       # Depends on: work_packages, users
   └── attachments    # Depends on: work_packages
   └── time_entries   # Depends on: work_packages, users

8. POST-PROCESSING
   └── inline_refs    # Updates references in descriptions
   └── admin_schemes  # Role memberships
   └── reporting      # Saved queries, dashboards
```

---

## Provenance Custom Fields

All migrated entities include provenance fields for traceability:

| Custom Field | Applies To | Purpose |
|--------------|------------|---------|
| J2O Origin Key | Work Package | e.g., "PROJ-123" |
| J2O Origin System | All entities | "jira" |
| J2O Origin URL | All entities | Link to Jira entity |
| Jira Issue ID | Work Package | Numeric Jira ID |
| Jira Project Key | Project | Original project key |
| Jira Project ID | Project | Numeric Jira ID |
| Jira User Key | User | Jira Server user key |
| Jira User ID | User | Jira account ID |

---

## Related Documentation

- [Migration Components Catalog](MIGRATION_COMPONENTS.md) - Component details
- [ADR-001: Two-Phase Migration](adr/ADR-001-two-phase-work-package-migration.md) - Architecture decision
- [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md) - Workflow configuration
- [Architecture Overview](ARCHITECTURE.md) - System design
