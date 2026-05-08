"""Domain enumerations.

These enums replace previously scattered magic strings used to discriminate
between domain concepts (e.g. journal entry kinds). They live in the domain
layer because they describe the j2o domain itself and are independent of any
I/O, framework, or external API.

``StrEnum`` is used so existing comparisons against the underlying string
values (and dict serialisation) keep working unchanged — ``StrEnum`` members
compare equal to their string values, which keeps the refactor non-behavioural
for any code path that still receives raw strings (e.g. payloads loaded from
JSON / Jira API responses).
"""

from enum import StrEnum


class JournalEntryType(StrEnum):
    """Discriminator for unified journal entries during work-package migration.

    A journal entry is either a Jira *comment* or a *changelog* history item;
    the two are merged into a single chronologically-ordered stream by the
    work-package migration before being written to OpenProject.
    """

    COMMENT = "comment"
    CHANGELOG = "changelog"


__all__ = ["JournalEntryType"]
