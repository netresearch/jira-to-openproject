# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read Simple Tasklists macros and their rendered checkboxes from the same issue description."""

import re
from html.parser import HTMLParser

_TASK_START = re.compile(r"\{task(?=[:}])")
_TASK_ID = re.compile(r"\{task:id=(\d+)\}", re.ASCII)


def _macro_titles(source: str) -> dict[str, str]:
    titles = {}
    opening = None
    for marker in _TASK_START.finditer(source):
        position = marker.start()
        if opening and source.startswith("{task}", position):
            titles[opening[1]] = source[opening.end() : position]
            opening = None
            continue
        current = _TASK_ID.match(source, position)
        if opening or current is None or current[1] in titles:
            msg = "Jira description contains an unsupported or duplicate task macro."
            raise ValueError(msg)
        opening = current
    if opening:
        msg = "Unclosed task macro in Jira description."
        raise ValueError(msg)
    return titles


class _DescriptionCheckboxes(HTMLParser):
    def __init__(self, issue_key: str) -> None:
        super().__init__()
        self.issue_key = issue_key
        self.containers: list[str | None] = []
        self.states: dict[str, bool] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "div":
            parent = self.containers[-1] if self.containers else None
            if "task" in classes:
                parent = attributes.get("data-task-id") if attributes.get("data-issue") == self.issue_key else None
            self.containers.append(parent)
        if tag == "input" and "task-input" in classes and attributes.get("type") == "checkbox":
            self._record_checkbox(attributes)

    def _record_checkbox(self, attributes: dict[str, str | None]) -> None:
        identifier = attributes.get("data-task-id")
        if not identifier or not self.containers or identifier != self.containers[-1]:
            return
        if identifier in self.states:
            msg = "Duplicate checkbox for a Jira description task."
            raise ValueError(msg)
        self.states[identifier] = "checked" in attributes

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.containers:
            self.containers.pop()


def description_tasks(issue_key: str, raw: dict) -> list[dict]:
    """Bind each raw task title to the checkbox in the rendered description using its source ID."""
    if raw.get("key") != issue_key:
        msg = "Jira returned a different issue for the description task request."
        raise ValueError(msg)
    source = raw["fields"]["description"] or ""
    titles = _macro_titles(source)
    if not titles:
        return []
    parser = _DescriptionCheckboxes(issue_key)
    parser.feed(raw["renderedFields"]["description"])
    parser.close()
    if titles.keys() != parser.states.keys():
        msg = "Jira description task IDs do not match the rendered checkboxes."
        raise ValueError(msg)
    return [{"id": key, "title": title, "checked": parser.states[key]} for key, title in titles.items()]
