# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Shared OpenProject description section formatting and delimiter checks."""

CHECKLIST_SECTION = r"""
checklist = lambda do |description, body, remove, prior|
  start_tag = "<!-- j2o:checklist:begin -->"
  end_tag = "<!-- j2o:checklist:end -->"
  raise "Checklist contains reserved delimiters" if [start_tag, end_tag].any? { |tag| body.include?(tag) }
  render = ->(md) { [start_tag, "## Jira checklist", md, end_tag].join("\n") }
  section = remove ? "" : render.call(body)
  if description.include?(start_tag) || description.include?(end_tag)
    before, opening, rest = description.partition(start_tag)
    inside, closing, after = rest.partition(end_tag)
    unless opening == start_tag && closing == end_tag &&
        (before.empty? || before.end_with?("\n")) && (after.empty? || after.start_with?("\n")) &&
        inside.start_with?("\n") && inside.end_with?("\n") &&
        description.scan(start_tag).one? && description.scan(end_tag).one?
      raise "Ambiguous checklist delimiters in work package description"
    end
    raise "Target checklist edited independently" if prior && Digest::SHA256.hexdigest(opening + inside + closing) != prior
    before + section + after
  else
    raise "Previously imported checklist is missing" if prior
    (remove || description.empty?) ? description + section : description + "\n\n" + section
  end
end
"""
