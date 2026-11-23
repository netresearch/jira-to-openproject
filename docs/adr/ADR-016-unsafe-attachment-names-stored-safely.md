# ADR-016: An attachment name the target refuses is stored under a safe name

## Status

Accepted (2026-11-23)

## Context

The Rails attach script refuses a stored attachment name that holds a control character, `/`, `\`, `;` or `"`, or that is longer than 255 bytes. OpenProject also sends the stored name as the HTTP download name, so these characters cannot be kept. Before this decision the script answered such a file with "Unsupported attachment filename", and the attachment was lost. The full run of 2026-11-22 lost 3 of 47,335 attachments this way: names with `//`, with `;` in a time stamp, and with quotation marks.

## Decision

J2O stores such an attachment under a safe name. Each refused character becomes `_`. A name over 255 bytes loses characters from the end of its stem until it is 8 bytes below the limit, so the extension and a later `(2)` suffix fit. A name left empty, `.` or `..` becomes `jira-attachment-<id>`. The Rails guard stays as the last check.

The renaming happens before the duplicate-name disambiguation. A file that keeps its Jira name keeps it in OpenProject too; a renamed file that meets that name yields and gets the `(2)` suffix. An earlier run stored only the files it did not refuse, under their own names, so a re-run finds them again and adds only the renamed files.

The local and container staging files use the same safe name, shortened to 200 bytes, because they carry a prefix and a `.part` suffix.

The attachment mapping keeps the Jira name as a second key, because Jira markup names the file by its original name. The recovery audit applies the same renaming, so it does not report a renamed file as missing.

## Consequences

- No attachment is lost because of its name. The OpenProject name differs from the Jira name in the refused characters.
- The import log names each renamed file with its issue key.
- A re-run finds the renamed file by its safe name and does not upload it again.
