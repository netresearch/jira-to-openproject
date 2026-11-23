# ADR-017: List field options come from the values J2O writes

## Status

Accepted (2026-11-23)

## Context

The custom field step read the options of Jira select and multiselect fields only from a ScriptRunner endpoint. Without ScriptRunner every list field was created with the single placeholder option "Default option" and as a single-value field. The content step then wrote the real Jira values, and the Rails writer refused each of them: "Missing or ambiguous option" for every label, and "Multiple values for single field" for every Jira array value. In a full run 43 list fields held only "Default option", and every value written to them was refused.

The custom field step also mapped the Jira Software field "Sprint" (`com.pyxis.greenhopper.jira:gh-sprint`) to a list field named "Sprint". The sprint and epic component owns sprint data and writes it as text into a field of that name. It found the list field by name and wrote text into it.

## Decision

Before the content step writes the custom field rows of a batch, it makes sure that each list field holds an option for every label those rows name. One Rails script handles all fields of the batch, each in its own transaction:

- It refuses any field that is not a work package custom field of format `list`.
- It appends each missing option, matching the value exactly, in the order the rows name it. Existing options without a position are numbered first, so the new ones come after them.
- It sets `multi_value` when a Jira field mapped to it has the type `array`.
- It removes the placeholder "Default option" only when the field has another option, no stored value references the placeholder, and no row names it. No other option is deleted or renamed.

The labels follow the writer's rule: a Jira option object gives `value`, else `name`; any other value gives itself; `null` is skipped. Values the writer refuses on purpose get no option: a cascading selection (an object with `child`), and fields of the Jira types `cascadingselect`, `project`, `version`, `multiversion` and `greenhopper-releasedmultiversionhistory`. Only list fields the custom field mapping holds take part. The writer's checks stay strict.

If OpenProject does not confirm a field, the rows of that field are not sent. They count as failed, and the log names the field ids. The log names the number of options created per field.

The Jira sprint field gets no target field. Its mapping entry carries `matched_by: sprint_epic` and no OpenProject id, so neither the content step nor the generic custom field step writes it. `ensure_wp_custom_field_id` refuses a same-named field of another format and names its id, its format and the requested format.

## Consequences

- The options are complete for the migrated data and need no ScriptRunner. Fields without an option set in Jira, such as labels fields, get their options too.
- An option defined in Jira but used by no migrated issue is not created.
- A list field holds a placeholder only while a stored value references it or no other option exists.
- A Jira array field becomes a multi-value field.
- A target that an earlier run gave a list field named "Sprint" stops the sprint field writes with an error naming that field, until the field is removed or renamed.
- Each batch with list values costs one more Rails call.
- A label OpenProject refuses as an option (longer than 255 characters) fails every row of that field in the batch.
