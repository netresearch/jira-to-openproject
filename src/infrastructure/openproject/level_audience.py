# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The staging check of a level sub-project after the late access grant (ADR-012).

Before the grant a level stage writes only into a sub-project without any membership row. After
it, the sub-project holds exactly the rows the grant writes, and a stage may still write: every
member is a reader of the level, so a new or changed object shows nothing to anyone who cannot
read the level's issues in Jira. ``level_audience`` accepts a sub-project whose rows are all such
grants: a user's project membership whose user is in ``members``, or a share of one of the
sub-project's work packages with a user in ``shares[<its J2O Origin Key>]``. Any other row (a
group, another user, a share of another work package or with another user, locked users
included) refuses. With an empty audience this is the memberless check it replaces.
"""

import json

from src.domain.provenance_fields import ORIGIN_KEY

LEVEL_AUDIENCE = """
level_audience = lambda do |project, audience|
  members = audience.fetch("members")
  shares = audience.fetch("shares")
  origin = WorkPackageCustomField.find_by(name: %s)
  Member.where(project_id: project.id).includes(:principal).all? do |m|
    if !m.principal.is_a?(User)
      false
    elsif m.entity_type.nil? && m.entity_id.nil?
      members.include?(m.user_id)
    elsif m.entity_type == "WorkPackage" && origin && WorkPackage.where(id: m.entity_id, project_id: project.id).exists?
      keys = CustomValue.where(customized_type: "WorkPackage", customized_id: m.entity_id, custom_field_id: origin.id).pluck(:value)
      keys.size == 1 && shares.fetch(keys.first, []).include?(m.user_id)
    else
      false
    end
  end
end
""" % json.dumps(ORIGIN_KEY)
