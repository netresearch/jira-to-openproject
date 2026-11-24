# ADR-018: A custom field becomes available where J2O writes a value

## Status

Accepted (2026-11-24)

## Context

OpenProject shows a work package custom field only where two links exist. The field must be linked to the type variant whose form configuration is in force for the work package's type in its project (`custom_fields_types`; OpenProject 17.9 links fields to type variants, earlier versions to types). Unless the field is for all projects (`is_for_all`), it must also be linked to the project (`custom_fields_projects`).

The custom field step links each new field that is for all projects to the base variant of every type that exists at that time. It runs before the issue type step, so the types that step creates later get no link. On the pilot, "Project Lead" was linked to the 38 types created before the field, and to none of the 56 types the issue type step created after it; Jira issues of such types, for example "Change Request", held values for it. In a full run the content step reported 26,126 refused custom field values. The log keeps the first three refusals of each batch: 194 of those 226 were "Field customFieldN unavailable for work package", the others named a missing option, a value that needs a mapping, or several values for a single field.

The per-field writer in `openproject_work_package_custom_field_service` already links a field to the form configuration owner and to the project before it writes a value. The row writer `batch_update_work_packages`, which the content step and several other components use, did not. It refused the value instead.

## Decision

Before `batch_update_work_packages` writes a row, it makes each custom field the row names available for that work package, in the same way as the per-field writer:

- It links the field to the form configuration owner in force for the work package (`wp.type_variant.effective_source_for(TypeVariant::FORM_CONFIGURATION)`, or `wp.type` before type variants).
- It links the field to the work package's project when the field is not for all projects.
- It drops OpenProject's request-local list of available fields for that project and type, so the writer sees the new link.

Each field, project and type is handled once per Rails call. The other checks of the writer stay as they are: an unknown field id, a missing list option, several values for a single field and an unmapped user are still refused.

## Consequences

### Positive

- A Jira value is not lost because the target type did not show the field.
- The type and project links follow the data, independent of the order of the components.

### Negative

- A field that is linked to a type shows on every work package of that type in the projects that use the type, also on those without a value.
- Links that J2O adds are not removed again if the value is later deleted.
